#!/usr/bin/env python3
"""fx_hang —— Sleep() 里睡 30s 不返回,验证 E_CALL_TIMEOUT。

sleep 卡在主线程,连接期间不处理任何消息,正是被测的"服务不响应"形态。
"""

from __future__ import annotations

import contextlib
import os
import sys
import time

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.example.FxHang"
OBJ_PATH = "/org/example/FxHang"
IFACE = "org.example.FxHang"
HANG_SECONDS = 30


class FxHang(dbus.service.Object):
    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def Sleep(self) -> None:
        time.sleep(HANG_SECONDS)


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_hang: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)
    bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    service = FxHang(bus, OBJ_PATH)
    print(f"fx_hang: 已注册 {bus_name.get_name()} @ {service.__dbus_object_path__}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
