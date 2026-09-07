#!/usr/bin/env python3
"""fx_wrongname —— 注册与配置期望不一致的名字,验证 E_READY_TIMEOUT 的"名字写错"分支。

配置里写 org.example.FxWrongName,进程实际拿到 org.example.FxUnexpected,
诊断应把总线上真实的名字列出来。
"""

from __future__ import annotations

import os
import sys

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.example.FxUnexpected"
OBJ_PATH = "/org/example/FxUnexpected"
IFACE = "org.example.FxUnexpected"


class FxUnexpected(dbus.service.Object):
    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Who(self) -> str:
        return BUS_NAME


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_wrongname: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)
    bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    service = FxUnexpected(bus, OBJ_PATH)
    print(
        f"fx_wrongname: 已注册 {bus_name.get_name()} @ {service.__dbus_object_path__}",
        flush=True,
    )
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
