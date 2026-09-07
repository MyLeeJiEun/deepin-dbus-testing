#!/usr/bin/env python3
"""fx_echo —— 六原语齐全的最小 fixture 服务(方法/属性/信号/错误)。

覆盖:call、get-prop、set-prop、wait-signal、introspect、错误路径。
属性接口 org.freedesktop.DBus.Properties 全部手写:dbus-python 各版本都不生成属性
反射与 Get/Set/GetAll,只能自己实现并把 <property> 注入 Introspect XML。
只从 DBUS_SESSION_BUS_ADDRESS 连总线,未设置即退出,避免污染开发桌面总线。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.example.FxEcho"
OBJ_PATH = "/org/example/FxEcho"
IFACE = "org.example.FxEcho"

# 属性表:名字 -> (签名, 是否可写)
PROPS: dict[str, tuple[str, bool]] = {"Counter": ("i", True), "Name": ("s", False)}


class BoomError(dbus.DBusException):
    _dbus_error_name = "org.example.FxEcho.Error.Boom"


class PropertyReadOnlyError(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.PropertyReadOnly"


class UnknownPropertyError(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.UnknownProperty"


class UnknownInterfaceError(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.UnknownInterface"


def properties_xml() -> str:
    """生成 <property> 反射片段,注入 dbus-python 自动生成的 Introspect XML。"""
    return "".join(
        '    <property name="{}" type="{}" access="{}"/>\n'.format(
            name, sig, "readwrite" if writable else "read"
        )
        for name, (sig, writable) in PROPS.items()
    )


class FxEcho(dbus.service.Object):
    """Echo/Sum/Boom/Ping + Counter/Name 属性 + Pinged 信号。"""

    def __init__(self, conn: BusConnection, path: str) -> None:
        super().__init__(conn, path)
        self.counter = 0
        self.name = "fx_echo"

    # ---- 业务方法 ----

    @dbus.service.method(IFACE, in_signature="s", out_signature="s")
    def Echo(self, text: str) -> str:
        return text

    @dbus.service.method(IFACE, in_signature="ai", out_signature="i")
    def Sum(self, values: Any) -> int:
        return sum(int(v) for v in values)

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Boom(self) -> None:
        raise BoomError("fx_echo 按约定抛出的错误")

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Ping(self) -> None:
        """无返回值,只发 Pinged 信号(计数器同步自增)。"""
        self.counter += 1
        self.Pinged(self.counter)

    @dbus.service.signal(IFACE, signature="i")
    def Pinged(self, count: int) -> None:
        """信号体不会被执行,由 dbus-python 装饰器负责发出。"""

    # ---- org.freedesktop.DBus.Properties(手写) ----

    def _check_iface(self, interface_name: str) -> None:
        # 空接口名是客户端常见的省略写法,按本对象唯一业务接口处理
        if interface_name not in ("", IFACE):
            raise UnknownInterfaceError(f"未知接口 {interface_name}")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name: str, property_name: str) -> Any:
        self._check_iface(interface_name)
        if property_name == "Counter":
            return dbus.Int32(self.counter)
        if property_name == "Name":
            return dbus.String(self.name)
        raise UnknownPropertyError(f"未知属性 {property_name}")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface_name: str, property_name: str, value: Any) -> None:
        self._check_iface(interface_name)
        if property_name not in PROPS:
            raise UnknownPropertyError(f"未知属性 {property_name}")
        if not PROPS[property_name][1]:
            raise PropertyReadOnlyError(f"属性 {property_name} 只读")
        self.counter = int(value)
        self.PropertiesChanged(IFACE, {"Counter": dbus.Int32(self.counter)}, [])

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name: str) -> dict[str, Any]:
        self._check_iface(interface_name)
        return {"Counter": dbus.Int32(self.counter), "Name": dbus.String(self.name)}

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(
        self, interface_name: str, changed: dict[str, Any], invalidated: list[str]
    ) -> None:
        """属性变更通知,由装饰器发出。"""

    # ---- Introspect:补上 dbus-python 不生成的 <property> ----

    @dbus.service.method(
        dbus.INTROSPECTABLE_IFACE,
        in_signature="",
        out_signature="s",
        path_keyword="object_path",
        connection_keyword="connection",
    )
    def Introspect(self, object_path: str, connection: Any) -> str:
        xml = dbus.service.Object.Introspect(self, object_path, connection)
        anchor = f'<interface name="{IFACE}">\n'
        head, sep, tail = xml.partition(anchor)
        if not sep:
            return xml
        return head + sep + properties_xml() + tail


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_echo: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)
    bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    service = FxEcho(bus, OBJ_PATH)
    print(f"fx_echo: 已注册 {bus_name.get_name()} @ {service.__dbus_object_path__}", flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
