"""systemd1 mock 扩展 —— 上游 python-dbusmock 的 `systemd` 模板不够用时使用。

为什么需要:上游 `systemd` 模板缺少 DDE 服务实际会调的方法。实测
dde-application-manager 在只挂上游模板时仍然 SIGABRT,mock 侧报
`UnknownMethod: Subscribe is not a valid method of interface
org.freedesktop.systemd1.Manager` —— 它在 `initService` 的依赖失败处 `std::terminate()`。

本模板补齐 AM 会用到的接口面(经源码核对:`Subscribe`、`ListUnitsByPatterns`、
`StartTransientUnit`,以及 `UnitNew`/`UnitRemoved`/`JobNew` 信号)。
它是**测试替身**,不模拟 systemd 的真实语义:
- `ListUnitsByPatterns` 默认返回空列表(即"当前没有已启动的应用单元");
- `StartTransientUnit` 返回一个假 job 路径并**不真的启动任何进程**;
- 单元属性通过 parameters 注入,默认没有任何单元。

用法:

    needs:
      - mock: systemd_dde

注入初始单元(可选):

    needs:
      - mock: systemd_dde
        params:
          units:
            - ["app-DDE-demo@1.service", "/org/freedesktop/systemd1/unit/demo"]
"""

import dbus

BUS_NAME = "org.freedesktop.systemd1"
MAIN_OBJ = "/org/freedesktop/systemd1"
MAIN_IFACE = "org.freedesktop.systemd1.Manager"
SYSTEM_BUS = False

UNIT_IFACE = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE = "org.freedesktop.systemd1.Service"

# ListUnits/ListUnitsByPatterns 的单元条目签名
UNIT_STRUCT = "(ssssssouso)"


def load(mock, parameters):  # noqa: ANN001, ANN201 - dbusmock 模板签名
    units = list(parameters.get("units") or []) if parameters else []
    mock.units = {}  # name -> object path

    mock.AddMethods(
        MAIN_IFACE,
        [
            # 事件订阅:AM 启动时第一件事就是调它,缺了直接 abort
            ("Subscribe", "", "", ""),
            ("Unsubscribe", "", "", ""),
            ("Reload", "", "", ""),
            # AM 用它枚举 app-*.service / app-*.scope
            ("ListUnitsByPatterns", "asas", f"a{UNIT_STRUCT}", "ret = []"),
            ("ListUnits", "", f"a{UNIT_STRUCT}", "ret = []"),
            # AM 用它拉起应用;测试替身只回一个假 job 路径,不真的起进程
            (
                "StartTransientUnit",
                "ssa(sv)a(sa(sv))",
                "o",
                'ret = dbus.ObjectPath("/org/freedesktop/systemd1/job/1")',
            ),
            ("StopUnit", "ss", "o", 'ret = dbus.ObjectPath("/org/freedesktop/systemd1/job/2")'),
            ("KillUnit", "ssi", "", ""),
            ("ResetFailedUnit", "s", "", ""),
            (
                "GetUnit",
                "s",
                "o",
                "ret = self.units.get(args[0]) or "
                'dbus.ObjectPath("/org/freedesktop/systemd1/unit/unknown")',
            ),
        ],
    )

    # AM 会读 Manager 的 Environment 属性来推导 PATH(applicationmanager1service.cpp:321),
    # 缺这条属性会让它在初始化阶段 abort。
    default_env = parameters.get("environment") if parameters else None
    environment = dbus.Array(
        default_env
        or [
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=C.UTF-8",
        ],
        signature="s",
    )
    mock.AddProperties(
        MAIN_IFACE,
        dbus.Dictionary(
            {
                "Version": "dbusmock-systemd-dde",
                "Virtualization": "",
                "Architecture": "x86-64",
                "Environment": environment,
                "UnitPath": dbus.Array([], signature="s"),
                "SystemState": "running",
            },
            signature="sv",
        ),
    )

    for name, path in units:
        add_unit(mock, name, path)


def add_unit(mock, name, path):  # noqa: ANN001, ANN201
    """注册一个单元对象,并发 UnitNew 信号(供测试驱动 AM 的实例发现逻辑)。"""
    mock.AddObject(
        path,
        UNIT_IFACE,
        dbus.Dictionary(
            {
                "Id": name,
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "LoadState": "loaded",
            },
            signature="sv",
        ),
        [],
    )
    unit = mock.GetObject(path)
    unit.AddProperties(
        SERVICE_IFACE,
        dbus.Dictionary({"MainPID": dbus.UInt32(0), "Result": "success"}, signature="sv"),
    )
    mock.units[name] = dbus.ObjectPath(path)
    mock.EmitSignal(MAIN_IFACE, "UnitNew", "so", [name, dbus.ObjectPath(path)])


def remove_unit(mock, name):  # noqa: ANN001, ANN201
    path = mock.units.pop(name, None)
    if path is not None:
        mock.EmitSignal(MAIN_IFACE, "UnitRemoved", "so", [name, path])
