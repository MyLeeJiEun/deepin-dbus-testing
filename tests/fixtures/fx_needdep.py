#!/usr/bin/env python3
"""fx_needdep —— 启动即调依赖服务上的方法,失败就 abort,验证 E_MOCK_UNFAITHFUL。

复刻 dde-application-manager 的实测行为(附录 A.1):initService 里依赖调用失败走
std::terminate,进程 SIGABRT。mock 不保真时 stderr 会带出
`UnknownMethod: Subscribe is not a valid method of interface ...`,诊断据此给出待补方法名。
依赖保真(mock 实现了 Subscribe)时正常注册自己的名字。
"""

from __future__ import annotations

import contextlib
import os
import sys

import dbus
import dbus.service
from dbus.bus import BusConnection
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.example.FxNeedDep"
OBJ_PATH = "/org/example/FxNeedDep"
IFACE = "org.example.FxNeedDep"

DEP_NAME = "org.example.FxDep"
DEP_PATH = "/org/example/FxDep"
DEP_IFACE = "org.example.FxDep.Manager"
DEP_METHOD = "Subscribe"
DEP_TIMEOUT = 5.0


class FxNeedDep(dbus.service.Object):
    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Who(self) -> str:
        return BUS_NAME


def main() -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        print("fx_needdep: DBUS_SESSION_BUS_ADDRESS 未设置,拒绝连接默认总线", file=sys.stderr)
        return 2
    bus = BusConnection(address, mainloop=DBusGMainLoop())
    bus.set_exit_on_disconnect(True)

    # 依赖前置:代理创建与调用都要包住 —— 名字不存在时 get_object 就会抛 ServiceUnknown;
    # introspect=False 让调用直接打到 DEP_METHOD 上,mock 的 UnknownMethod 原样上浮。
    try:
        dep = dbus.Interface(bus.get_object(DEP_NAME, DEP_PATH, introspect=False), DEP_IFACE)
        dep.get_dbus_method(DEP_METHOD)(timeout=DEP_TIMEOUT)
    except dbus.DBusException as exc:
        print(
            f"fx_needdep: 依赖 {DEP_IFACE}.{DEP_METHOD} 调用失败 "
            f"{exc.get_dbus_name()}: {exc.get_dbus_message()}",
            file=sys.stderr,
        )
        sys.stderr.flush()
        os.abort()  # 复刻 std::terminate:不给收尾机会,直接 SIGABRT

    bus_name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    service = FxNeedDep(bus, OBJ_PATH)
    print(f"fx_needdep: 已注册 {bus_name.get_name()} @ {service.__dbus_object_path__}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
