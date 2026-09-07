#!/usr/bin/env python3
"""fx_readonly —— 只有一个只读属性,验证 set-prop 的错误路径。

Set 一律抛 org.freedesktop.DBus.Error.PropertyReadOnly;
Properties 接口手写,<property> 反射注入 Introspect XML(dbus-python 不生成)。
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.example.FxReadonly"
OBJ_PATH = "/org/example/FxReadonly"
IFACE = "org.example.FxReadonly"

PROP_NAME = "Fixed"
PROP_VALUE = "fixed-value"


class PropertyReadOnlyError(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.PropertyReadOnly"


class UnknownPropertyError(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.UnknownProperty"


class UnknownInterfaceError(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.UnknownInterface"


class FxReadonly(dbus.service.Object):
    """唯一成员:只读属性 Fixed(s)。"""

    def _check_iface(self, interface_name: str) -> None:
        if interface_name not in ("", IFACE):
            raise UnknownInterfaceError(f"未知接口 {interface_name}")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name: str, property_name: str) -> Any:
        self._check_iface(interface_name)
        if property_name != PROP_NAME:
            raise UnknownPropertyError(f"未知属性 {property_name}")
        return dbus.String(PROP_VALUE)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface_name: str, property_name: str, value: Any) -> None:
        self._check_iface(interface_name)
        if property_name != PROP_NAME:
            raise UnknownPropertyError(f"未知属性 {property_name}")
        raise PropertyReadOnlyError(f"属性 {property_name} 只读")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name: str) -> dict[str, Any]:
        self._check_iface(interface_name)
        return {PROP_NAME: dbus.String(PROP_VALUE)}

    @dbus.service.method(
        dbus.INTROSPECTABLE_IFACE,
        in_signature="",
        out_signature="s",
        path_keyword="object_path",
        connection_keyword="connection",
    )
    def Introspect(self, object_path: str, connection: Any) -> str:
        xml = dbus.service.Object.Introspect(self, object_path, connection)
        # dbus-python 不会为没有方法/信号的接口生成 <interface>,只能整段插入
        anchor = "</node>"
        prop = (
            f'  <interface name="{IFACE}">\n'
            f'    <property name="{PROP_NAME}" type="s" access="read"/>\n'
            "  </interface>\n"
        )
        head, sep, tail = xml.rpartition(anchor)
        if not sep:
            return xml
        return head + prop + sep + tail


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_readonly: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)
    bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    service = FxReadonly(bus, OBJ_PATH)
    print(f"fx_readonly: 已注册 {bus_name.get_name()} @ {service.__dbus_object_path__}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
