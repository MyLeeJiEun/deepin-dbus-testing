import dbus

"""org.desktopspec.ConfigManager(dconfig-daemon)替身。

deepin-authentication 等系统服务在 init 阶段会通过 go-dbus-factory 的
org.desktopspec.ConfigManager 客户端调用 `AcquireManager` 获取指定配置模块的
Manager 对象,然后读/写 key。私有 system bus 上没有 dde-dconfig-daemon,
必须用本模板补齐,否则被测服务在 `acquireManager` 处直接拿空对象、随后
nil 解引用崩掉(deepin-authentication 实测 SPAN:manager.go getLimitDConfigByKey)。

接口面依据 go-dbus-factory(2026-05 版)org.desktopspec.ConfigManager/auto.go:
- org.desktopspec.ConfigManager @ /            : acquireManager(sss)->o
                                                  acquireManagerV2(usss)->o
                                                  removeUserData(u)
- org.desktopspec.ConfigManager.Manager @ <acquired>:
      value(s)->v / setValue(sv) / isDefaultValue(s)->b / reset(s)
      name(ss)->s / description(ss)->s / visibility(s)->s
      permissions(s)->s / flags(s)->i / release()
      signal valueChanged(s); 属性 Version(s)、KeyList(as)

现在所有 key 都返回空值(JSON "[]"),被测服务照常走"默认配置"分支,
不会误触发 mock 不一致。
"""

BUS_NAME = "org.desktopspec.ConfigManager"
MAIN_OBJ = "/"
MAIN_IFACE = "org.desktopspec.ConfigManager"
MANAGER_IFACE = "org.desktopspec.ConfigManager.Manager"
SYSTEM_BUS = True

_DEFAULT_MANAGER = "/org/desktopspec/ConfigManager/manager/0"

# 对应 authentic/config 里 LimitConfig 的 JSON 数组 -> 空数组
_EMPTY_LIST_JSON = "[]"
_DEFAULT_VERSION = ""


def load(mock, parameters):  # noqa: ANN001, ANN201 - dbusmock 模板签名
    # spawn 自动创建 MAIN_OBJ("/") 上的主对象,这里只补方法
    mock.AddMethods(
        MAIN_IFACE,
        [
            ("acquireManager", "sss", "o", f'ret = "{_DEFAULT_MANAGER}"'),
            ("acquireManagerV2", "usss", "o", f'ret = "{_DEFAULT_MANAGER}"'),
            ("removeUserData", "u", "", ""),
        ],
    )

    # 注意:dbusmock 的 AddMethods(iface, ...) 只作用于"当前对象",
    # 而 AddObject 是创建新对象并切换到它 —— 所以 Manager 方法的签名
    # 必须直接作为 AddObject 的第 4 个参数传入,事后再 AddMethods
    # 只会改回主对象,Manager 对象仍然一个方法都没有(dde-appearance CI 实测)。
    mock.AddObject(
        _DEFAULT_MANAGER,
        MANAGER_IFACE,
        {
            "Version": _DEFAULT_VERSION,
            "KeyList": dbus.Array([], signature="s"),
        },
        [
            # dbusmock exec 环境里没有 dbus.Variant,直接用字符串;
            # DTK/go-dconfig 客户端拿到底层 a{sv} JSON 后自行反序列化
            ("value", "s", "v", f'ret = "{_EMPTY_LIST_JSON}"'),
            ("setValue", "sv", "", ""),
            ("isDefaultValue", "s", "b", "ret = False"),
            ("reset", "s", "", ""),
            ("name", "ss", "s", 'ret = ""'),
            ("description", "ss", "s", 'ret = ""'),
            ("visibility", "s", "s", 'ret = "readwrite"'),
            ("permissions", "s", "s", 'ret = "readwrite"'),
            ("flags", "s", "i", "ret = 0"),
            ("release", "", "", ""),
            ("signal valueChanged", "s", "", ""),
        ],
    )
