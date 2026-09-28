#!/usr/bin/env python3
"""fx_twin —— 单进程注册两个名字,同一路径 `/` 按 destination 各导不同接口。

专门钉住 snapshot 的多名字共享路径合并语义(见 scanner/introspect.py):
不同名字在同一连接、同一路径上的 introspect 应答可以不同,这是合法 D-Bus 行为,
基线必须把两个名字的接口都收进来。
"""

from __future__ import annotations

import os
import sys

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

NAMES = ("org.example.TwinA", "org.example.TwinB")
XML = {
    "org.example.TwinA": (
        '<node><interface name="org.example.TwinA">'
        '<method name="WhoA"><arg direction="out" type="s"/></method>'
        "</interface></node>"
    ),
    "org.example.TwinB": (
        '<node><interface name="org.example.TwinB">'
        '<method name="WhoB"><arg direction="out" type="s"/></method>'
        "</interface></node>"
    ),
}


class Twin(dbus.service.Object):
    def __init__(self, conn: BusConnection) -> None:
        super().__init__(conn, "/")

    @dbus.service.method(
        "org.freedesktop.DBus.Introspectable",
        in_signature="",
        out_signature="s",
        destination_keyword="dest",
    )
    def Introspect(self, dest: str | None = None) -> str:
        return XML.get(str(dest), "<node/>")


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_twin: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    DBusGMainLoop(set_as_default=True)
    bus = BusConnection(address)
    obj = Twin(bus)  # noqa: F841 - 单对象,两个名字共用,按 destination 区分应答
    keep = [dbus.service.BusName(n, bus, do_not_queue=True) for n in NAMES]  # noqa: F841
    print("fx_twin: 已注册 " + " ".join(NAMES), flush=True)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
