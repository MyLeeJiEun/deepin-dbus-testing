#!/usr/bin/env python3
"""fx_multi —— 单进程注册 3 个服务名,验证 services: 多名字与 ready 全就绪。

三个名字共用一条连接,各自导出一个对象;Who() 回答自己代表哪个名字。
"""

from __future__ import annotations

import os
import sys

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAMES = ("org.example.FxMultiA", "org.example.FxMultiB", "org.example.FxMultiC")
IFACE = "org.example.FxMulti"


class FxMulti(dbus.service.Object):
    """一个对象对应一个服务名,Who() 返回该名字。"""

    def __init__(self, conn: BusConnection, path: str, who: str) -> None:
        super().__init__(conn, path)
        self.who = who

    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Who(self) -> str:
        return self.who


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_multi: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)
    # 名字与对象都必须保持引用,否则被回收会连带释放总线名
    owned = []
    for name in BUS_NAMES:
        path = "/" + name.replace(".", "/")
        owned.append((dbus.service.BusName(name, bus, do_not_queue=True), FxMulti(bus, path, name)))
    print("fx_multi: 已注册 " + " ".join(n.get_name() for n, _ in owned), flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
