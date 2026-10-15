import dbus
from typing import Any

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


def _to_variant_literal(value: Any) -> str:
    """把 params.values 里的原生值转成 dbusmock exec 代码的 Variant 构造串。

    支持:bool -> dbus.Boolean、int -> dbus.Int32、float -> dbus.Double、
    str -> dbus.String、list -> JSON 字符串(a{sv} 客户端按约定自解)。
    dbusmock 的 exec 环境有 dbus 模块(dbus-python 装了它才有),Boolean/
    Int32/Double/String 均可直接用;只有 dbus.Variant 不可用(实测)。
    """
    if isinstance(value, bool):
        return f"dbus.Boolean({value})"
    if isinstance(value, int):
        return f"dbus.Int32({value})"
    if isinstance(value, float):
        return f"dbus.Double({value})"
    if isinstance(value, str):
        return f"dbus.String({value!r})"
    # list/dict 等:JSON 序列化成字符串,与 _EMPTY_LIST_JSON 的兜底同路
    import json

    return f"dbus.String({json.dumps(value, ensure_ascii=False)!r})"


def _encode_value(value: Any) -> str:
    """把原生值编码为类型化字符串(b:/i:/d:/s:/j:),见 load() 注释。"""
    if isinstance(value, bool):
        return f"b:{'true' if value else 'false'}"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, float):
        return f"d:{value}"
    if isinstance(value, str):
        return f"s:{value}"
    import json
    return f"j:{json.dumps(value, ensure_ascii=False)}"


def load(mock, parameters):  # noqa: ANN001, ANN201 - dbusmock 模板签名
    # params.values: key -> 类型化编码字符串。**为什么是字符串编码而非原生值**:
    # dbusmock 的 parameters 走 a{sv} D-Bus 传输,实测所有标量都被改写成
    # dbus.Boolean(True)(类型信息全丢);字符串是唯一保真的载体。
    # 编码格式(见 _encode_value): "b:true" / "i:3" / "d:1.5" / "s:info"
    #   / "j:<json>"(列表/对象)。未列出的 key 走空 JSON 兜底。
    values: dict[str, str] = {}
    for k, v in dict(parameters.get("values") or {}).items():
        if isinstance(v, dbus.String):
            v = str(v)
        if not isinstance(v, str):
            # 兜容:调用方仍传了原生值(单进程内直连场景),当场编码
            v = _encode_value(v)
        values[str(k)] = v

    # 生成查表 exec 代码。exec 环境(实测):
    #   - 入参在 args 列表里(args[0] = key),无具名形参
    #   - globals() 是 dbusmock.mockobject 模块,不可依赖 import
    if values:
        lit_map = "{" + ", ".join(f"{k!r}: {v!r}" for k, v in values.items()) + "}"
        value_impl = (
            f"_m = {lit_map}; _e = _m.get(args[0], 'j:{_EMPTY_LIST_JSON}'); "
            "_t, _p = _e.split(':', 1); "
            "ret = (dbus.Boolean(_p == 'true') if _t == 'b' else "
            "(dbus.Int32(int(_p)) if _t == 'i' else "
            "(dbus.Double(float(_p)) if _t == 'd' else dbus.String(_p))))"
        )
    else:
        value_impl = f'ret = "{_EMPTY_LIST_JSON}"'

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
            # 按 params.values 精确返回(类型正确),未配置的 key 返回空 JSON 兜底
            ("value", "s", "v", value_impl),
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
