"""DBus 客户端:方法调用、属性读写、信号捕获、introspect 与对象树遍历.

实现要点(均有实测依据,见开发文档附录 A):
- 取回复签名必须用低层 API(`send_message_with_reply_and_block` + `Message.get_signature()`)。
- 信号分发必须**单线程**:后台线程跑 GLib MainLoop 与主线程阻塞调用并发会 SIGSEGV;
  这里统一用手工迭代 `GLib.MainContext` 的 `wait_until` 实现等待。
- 值断言前双方都过 `to_native`,消除 dbus.* 包装类型差异。
- `get_prop` 的 `CallOutcome.signature` 填**属性本身的类型码**(而不是外层 variant 的 "v"),
  这样 `expect.type` / `expect.signature` 在属性用例里可直接使用。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from xml.etree import ElementTree

from ..errors import BusError, CallTimeout
from ..model import Target
from ..results import CallOutcome

log = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖发行版包
    import dbus
    import dbus.lowlevel
    from dbus.mainloop.glib import DBusGMainLoop

    _DBUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    dbus = None  # type: ignore[assignment]
    DBusGMainLoop = None  # type: ignore[assignment]
    _DBUS_IMPORT_ERROR = exc

try:  # pragma: no cover - 依赖发行版包
    from gi.repository import GLib

    _GLIB_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    GLib = None  # type: ignore[assignment]
    _GLIB_IMPORT_ERROR = exc

DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECTABLE_IFACE = "org.freedesktop.DBus.Introspectable"

MAX_WALK_DEPTH = 32

_mainloop_ready = False


def _require_dbus() -> None:
    if _DBUS_IMPORT_ERROR is not None:
        raise BusError(f"缺少 dbus-python(python3-dbus),无法与总线通信: {_DBUS_IMPORT_ERROR}")


def _ensure_mainloop() -> None:
    """必须在建立连接**之前**绑定默认主循环,否则信号不会被分发。"""
    global _mainloop_ready
    if _mainloop_ready:
        return
    _require_dbus()
    if _GLIB_IMPORT_ERROR is not None:
        raise BusError(f"缺少 PyGObject(python3-gi),无法分发 DBus 信号: {_GLIB_IMPORT_ERROR}")
    DBusGMainLoop(set_as_default=True)
    _mainloop_ready = True


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.01) -> bool:
    """单线程等待:迭代默认 GLib 上下文直到 predicate 为真或超时。"""
    _ensure_mainloop()
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        while ctx.pending():
            ctx.iteration(may_block=False)
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return bool(predicate())
        time.sleep(interval)


# --------------------------------------------------------------------------- 类型转换

_SCALAR_CODES: tuple[tuple[str, str], ...] = (
    ("Boolean", "b"),
    ("Byte", "y"),
    ("Int16", "n"),
    ("UInt16", "q"),
    ("Int32", "i"),
    ("UInt32", "u"),
    ("Int64", "x"),
    ("UInt64", "t"),
    ("Double", "d"),
    ("ObjectPath", "o"),
    ("Signature", "g"),
    ("String", "s"),
)


def to_native(value: Any) -> Any:
    """把 dbus.* 包装类型递归转为 Python 原生类型。"""
    if _DBUS_IMPORT_ERROR is not None:  # pragma: no cover
        return value
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, dbus.ByteArray):
        return bytes(value)
    if isinstance(
        value,
        (dbus.Byte, dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64),
    ):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    if isinstance(value, (dbus.ObjectPath, dbus.Signature, dbus.String)):
        return str(value)
    if isinstance(value, dbus.Dictionary):
        return {to_native(k): to_native(v) for k, v in value.items()}
    if isinstance(value, dbus.Struct):
        return tuple(to_native(v) for v in value)
    if isinstance(value, dbus.Array):
        return [to_native(v) for v in value]
    if isinstance(value, dict):
        return {to_native(k): to_native(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(to_native(v) for v in value)
    if isinstance(value, list):
        return [to_native(v) for v in value]
    return value


def dbus_type_code(value: Any) -> str:
    """推断一个 dbus.* 值的 DBus 类型码(用于属性类型断言)。"""
    if _DBUS_IMPORT_ERROR is not None:  # pragma: no cover
        return ""
    for cls_name, code in _SCALAR_CODES:
        cls = getattr(dbus, cls_name, None)
        if cls is not None and isinstance(value, cls):
            return code
    if isinstance(value, dbus.Dictionary):
        sig = getattr(value, "signature", None)
        if sig:
            return "a{" + str(sig) + "}"
        for k, v in value.items():
            return "a{" + dbus_type_code(k) + dbus_type_code(v) + "}"
        return "a{sv}"
    if isinstance(value, dbus.Struct):
        return "(" + "".join(dbus_type_code(v) for v in value) + ")"
    if isinstance(value, dbus.Array):
        sig = getattr(value, "signature", None)
        if sig:
            return "a" + str(sig)
        for item in value:
            return "a" + dbus_type_code(item)
        return "av"
    # 已经是原生类型(不常见,兜底)
    if isinstance(value, bool):
        return "b"
    if isinstance(value, int):
        return "i"
    if isinstance(value, float):
        return "d"
    if isinstance(value, str):
        return "s"
    if isinstance(value, (list, tuple)):
        return "av"
    if isinstance(value, dict):
        return "a{sv}"
    return ""


def unwrap_single(value: Any) -> Any:
    """DBus 返回值总是列表;单返回值时解包,零返回值时返回 None。"""
    if isinstance(value, list):
        if not value:
            return None
        if len(value) == 1:
            return value[0]
    return value


# --------------------------------------------------------------------------- 客户端


class BusClient:
    """一条到指定总线地址的连接。所有交互都走这条连接(避免跨连接同步调用死锁)。"""

    def __init__(self, address: str) -> None:
        _ensure_mainloop()
        self.address = address
        try:
            self._conn = dbus.bus.BusConnection(address)
        except Exception as exc:  # pragma: no cover - 地址非法/总线已退出
            raise BusError(f"无法连接总线 {address}: {exc}") from exc

    # ------------------------------------------------------------------ 名字

    def name_has_owner(self, name: str) -> bool:
        try:
            return bool(self._conn.name_has_owner(name))
        except Exception:  # pragma: no cover - 总线异常
            return False

    def list_acquired_names(self) -> list[str]:
        """总线上已被持有的 well-known 名字(过滤掉 :1.x 唯一名)。"""
        out = self.call(Target(DBUS_SERVICE, DBUS_PATH, DBUS_IFACE, "ListNames"), (), timeout=5.0)
        names = out.value if isinstance(out.value, list) else []
        return sorted(n for n in names if isinstance(n, str) and not n.startswith(":"))

    # ------------------------------------------------------------------ 底层调用

    def _invoke_raw(
        self,
        target: Target,
        args: tuple[Any, ...],
        timeout: float,
        signature: str | None = None,
    ) -> tuple[bool, list[Any], str, str | None, str | None, float]:
        _require_dbus()
        started = time.monotonic()
        msg = dbus.lowlevel.MethodCallMessage(
            target.service, target.path, target.interface, target.member
        )
        if args:
            try:
                msg.append(*args, signature=signature)
            except Exception as exc:
                raise BusError(
                    f"参数无法序列化(签名 {signature!r}): {exc};"
                    f"目标 {target.interface}.{target.member},参数 {args!r}"
                ) from exc

        try:
            # dbus-python 的 timeout 单位是**秒**(float),不是毫秒;实测确认。
            reply = self._conn.send_message_with_reply_and_block(msg, float(timeout))
        except dbus.exceptions.DBusException as exc:
            elapsed = time.monotonic() - started
            name = exc.get_dbus_name() or "org.freedesktop.DBus.Error.Failed"
            if name == "org.freedesktop.DBus.Error.NoReply":
                raise CallTimeout(
                    f"调用 {target.interface}.{target.member} 超时({timeout}s 无回复)",
                    target=f"{target.interface}.{target.member}",
                    timeout=timeout,
                ) from exc
            return False, [], "", name, str(exc), elapsed

        elapsed = time.monotonic() - started
        return True, list(reply.get_args_list()), str(reply.get_signature()), None, None, elapsed

    # ------------------------------------------------------------------ 公开操作

    def call(self, target: Target, args: tuple[Any, ...] = (), timeout: float = 5.0) -> CallOutcome:
        ok, raw, sig, err, msg, elapsed = self._invoke_raw(target, args, timeout)
        if not ok:
            return CallOutcome(False, None, "", err, msg, elapsed)
        return CallOutcome(True, unwrap_single(to_native(raw)), sig, None, None, elapsed)

    def get_prop(self, target: Target, timeout: float = 5.0) -> CallOutcome:
        props = Target(target.service, target.path, PROPERTIES_IFACE, "Get")
        ok, raw, _sig, err, msg, elapsed = self._invoke_raw(
            props, (target.interface, target.member), timeout, signature="ss"
        )
        if not ok:
            return CallOutcome(False, None, "", err, msg, elapsed)
        inner = raw[0] if raw else None
        return CallOutcome(True, to_native(inner), dbus_type_code(inner), None, None, elapsed)

    def get_all_props(self, target: Target, timeout: float = 5.0) -> CallOutcome:
        props = Target(target.service, target.path, PROPERTIES_IFACE, "GetAll")
        ok, raw, sig, err, msg, elapsed = self._invoke_raw(
            props, (target.interface,), timeout, signature="s"
        )
        if not ok:
            return CallOutcome(False, None, "", err, msg, elapsed)
        return CallOutcome(True, unwrap_single(to_native(raw)), sig, None, None, elapsed)

    def set_prop(self, target: Target, value: Any, timeout: float = 5.0) -> CallOutcome:
        props = Target(target.service, target.path, PROPERTIES_IFACE, "Set")
        variant = _guess_variant(value)
        ok, raw, sig, err, msg, elapsed = self._invoke_raw(
            props, (target.interface, target.member, variant), timeout, signature="ssv"
        )
        if not ok:
            return CallOutcome(False, None, "", err, msg, elapsed)
        return CallOutcome(True, unwrap_single(to_native(raw)), sig, None, None, elapsed)

    # ------------------------------------------------------------------ introspect

    def introspect(self, service: str, path: str, timeout: float = 5.0) -> str:
        out = self.call(Target(service, path, INTROSPECTABLE_IFACE, "Introspect"), (), timeout)
        if not out.ok:
            raise BusError(
                f"introspect {service} {path} 失败: {out.error_name}: {out.error_message}"
            )
        return str(out.value or "")

    def walk(
        self,
        service: str,
        root: str = "/",
        *,
        decide: Callable[[str], str] | None = None,
        timeout: float = 5.0,
    ) -> Iterator[tuple[str, str]]:
        """递归遍历对象树,产出 (path, xml)。

        decide(path) 返回三态:
          "keep"  —— 正常 introspect 并继续向下展开(默认);
          "prune" —— introspect 该路径(留一份样本供归并),但**不**再向下展开;
          "skip"  —— 完全不碰该分支。
        """
        pending: list[tuple[str, int, bool]] = [(root, 0, True)]
        seen: set[str] = set()
        while pending:
            path, depth, descend = pending.pop(0)
            if path in seen or depth > MAX_WALK_DEPTH:
                continue
            seen.add(path)
            try:
                xml = self.introspect(service, path, timeout=timeout)
            except BusError as exc:
                log.debug("跳过无法 introspect 的路径 %s: %s", path, exc)
                continue
            yield path, xml
            if not descend:
                continue
            for child in _child_nodes(xml):
                child_path = ("" if path == "/" else path) + "/" + child
                verdict = decide(child_path) if decide is not None else "keep"
                if verdict == "skip":
                    continue
                pending.append((child_path, depth + 1, verdict != "prune"))

    # ------------------------------------------------------------------ 信号

    @contextmanager
    def capture(self, target: Target) -> Iterator[list[Any]]:
        """进入即布扣(必须在触发动作之前),退出时移除匹配。"""
        _require_dbus()
        received: list[Any] = []

        def handler(*args: Any, **_kw: Any) -> None:
            received.append(unwrap_single(to_native(list(args))))

        match = self._conn.add_signal_receiver(
            handler,
            signal_name=target.member,
            dbus_interface=target.interface,
            path=target.path,
        )
        try:
            yield received
        finally:
            try:
                match.remove()
            except Exception:  # pragma: no cover - 连接已断
                pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - 已关闭
            pass


def _guess_variant(value: Any) -> Any:
    """为 Properties.Set 推断 variant 类型(声明式配置里只有 YAML 标量)。"""
    _require_dbus()
    if isinstance(value, bool):
        return dbus.Boolean(value, variant_level=1)
    if isinstance(value, int):
        return dbus.Int32(value, variant_level=1)
    if isinstance(value, float):
        return dbus.Double(value, variant_level=1)
    if isinstance(value, str):
        return dbus.String(value, variant_level=1)
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, str) for v in value):
            return dbus.Array(value, signature="s", variant_level=1)
        return dbus.Array(value, signature="v", variant_level=1)
    if isinstance(value, dict):
        return dbus.Dictionary(value, signature="sv", variant_level=1)
    if value is None:
        return dbus.String("", variant_level=1)
    return value


def _child_nodes(xml: str) -> list[str]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    return [node.get("name", "") for node in root.findall("node") if node.get("name")]
