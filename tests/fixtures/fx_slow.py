#!/usr/bin/env python3
"""fx_slow —— 延迟注册名字,验证 E_READY_TIMEOUT 与实际名字列表。

默认延迟 30s(远超 ready 超时);FX_SLOW_DELAY 可调,自测里设小值走成功路径。
"""

from __future__ import annotations

import os
import sys
import time

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.example.FxSlow"
OBJ_PATH = "/org/example/FxSlow"
IFACE = "org.example.FxSlow"
DEFAULT_DELAY = 30.0


class FxSlow(dbus.service.Object):
    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Who(self) -> str:
        return BUS_NAME


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_slow: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    delay = float(os.environ.get("FX_SLOW_DELAY") or DEFAULT_DELAY)
    print(f"fx_slow: 启动,{delay}s 后才注册 {BUS_NAME}", flush=True)
    # 先连总线再睡:连接本身不占名字,ready 判定只看名字是否被 acquired
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)
    time.sleep(delay)
    bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    service = FxSlow(bus, OBJ_PATH)
    print(f"fx_slow: 已注册 {bus_name.get_name()} @ {service.__dbus_object_path__}", flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
