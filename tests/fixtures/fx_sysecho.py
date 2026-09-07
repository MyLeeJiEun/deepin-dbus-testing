#!/usr/bin/env python3
"""自测用:注册在 **system bus** 上的 fixture 服务。

从环境变量 DBUS_SYSTEM_BUS_ADDRESS 连总线(未设置时拒绝启动,
防止落到真实 system bus)——这与框架对 system-bus: true 的注入方式一致。
"""

import os
import sys
from typing import Any

addr = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
if not addr:
    sys.stderr.write("fx_sysecho: DBUS_SYSTEM_BUS_ADDRESS 未设置,拒绝连接默认总线\n")
    sys.exit(2)

import dbus  # noqa: E402
import dbus.service  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

DBusGMainLoop(set_as_default=True)

BUS_NAME = "org.example.SysEcho"
PATH = "/org/example/SysEcho"
IFACE = "org.example.SysEcho"


class SysEcho(dbus.service.Object):
    def __init__(self, conn: dbus.connection.Connection) -> None:
        self._props = {"Count": 0}
        super().__init__(conn, PATH)

    # ---- 方法 ----
    @dbus.service.method(IFACE, in_signature="s", out_signature="s")
    def Echo(self, text: str) -> str:
        return f"sys:{text}"

    # ---- 信号 ----
    @dbus.service.signal(IFACE, signature="i")
    def Counted(self, value: int) -> None:
        pass

    # ---- Properties(手写,兼容所有 dbus-python 版本)----
    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, name: str) -> str:
        if interface != IFACE or name not in self._props:
            raise dbus.exceptions.DBusException(
                f"{interface}.{name}",
                name="org.freedesktop.DBus.Error.UnknownProperty",
            )
        stored = self._props[name]
        if isinstance(stored, bool):
            wrapped = dbus.Boolean(stored)
        elif isinstance(stored, int):
            wrapped = dbus.Int32(stored)
        elif isinstance(stored, float):
            wrapped = dbus.Double(stored)
        else:
            wrapped = dbus.String(stored)
        return wrapped  # variant_level 由 Properties.Get 的 v 签名隐含

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict:
        if interface != IFACE:
            raise dbus.exceptions.DBusException(
                interface, name="org.freedesktop.DBus.Error.UnknownInterface"
            )
        return dbus.Dictionary(dict(self._props), signature="sv")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface: str, name: str, value: Any) -> None:
        if interface != IFACE or name not in self._props:
            raise dbus.exceptions.DBusException(
                f"{interface}.{name}",
                name="org.freedesktop.DBus.Error.UnknownProperty",
            )
        self._props[name] = int(value.value)
        self.Counted(int(value.value))


def main() -> int:
    try:
        bus = dbus.SystemBus()
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(f"fx_sysecho: 连接 system bus 失败: {exc}\n")
        return 3
    SysEcho(bus)

    if not bus.request_name(BUS_NAME):
        sys.stderr.write(f"fx_sysecho: 注册 {BUS_NAME} 失败\n")
        return 4
    print(f"{BUS_NAME}: 已注册 @ {PATH}", flush=True)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
