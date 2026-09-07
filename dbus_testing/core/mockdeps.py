"""依赖 mock 编排(基于 python-dbusmock).

顺序强制:mock 必须在拉起被测进程**之前**全部就绪。
实测依据:dde-application-manager 在缺少 org.freedesktop.systemd1 时直接 std::terminate。

不保真检测:mock 进程输出里出现 `UnknownMethod: <X> is not a valid method of interface <I>`
时抛 MockUnfaithful,由 diagnose 输出待补方法名与模板片段。
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from pathlib import Path
from typing import Any

from ..errors import MockUnfaithful
from ..model import MockSpec

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "mocktemplates"

# 兼容 dbus-python 的两种措辞:
#   "UnknownMethod: Subscribe is not a valid method of interface X"
#   "UnknownMethod: Unknown method: Subscribe is not a valid method of interface X"
_UNKNOWN_METHOD_RE = re.compile(
    r"(?P<method>[A-Za-z0-9_]+) is not a valid method of interface (?P<iface>[A-Za-z0-9_.\-]+)"
)


def scan_missing_methods(text: str) -> list[str]:
    """从任意输出文本里提取"mock 缺失方法"的证据(mock 侧或被测服务侧都可能报)。"""
    out: list[str] = []
    for m in _UNKNOWN_METHOD_RE.finditer(text or ""):
        full = f"{m.group('iface')}.{m.group('method')}"
        if full not in out:
            out.append(full)
    return out


class _MockProcess:
    def __init__(self, spec: MockSpec, spawned: Any) -> None:
        self.spec = spec
        self.spawned = spawned
        self.lines: deque[str] = deque(maxlen=200)
        self._readers: list[threading.Thread] = []
        for attr in ("stdout", "stderr"):
            stream = getattr(spawned, attr, None)
            if stream is not None:
                thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
                thread.start()
                self._readers.append(thread)

    def _drain(self, stream: Any) -> None:
        for raw in stream:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            self.lines.append(text.rstrip("\n"))

    def missing_methods(self) -> list[str]:
        return scan_missing_methods("\n".join(self.lines))

    def terminate(self) -> None:
        proc = getattr(self.spawned, "process", None)
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - 已退出
            pass


class MockDeps:
    """按 needs: 启动依赖 mock。"""

    def __init__(self, specs: tuple[MockSpec, ...]) -> None:
        self.specs = specs
        self._running: list[_MockProcess] = []

    @property
    def active(self) -> bool:
        return bool(self.specs)

    def start(self, session_address: str, system_address: str | None = None) -> None:
        if not self.specs:
            return
        try:
            import dbusmock
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise MockUnfaithful(
                "配置声明了 needs:(依赖 mock),但未安装 python-dbusmock"
                "(deb: python3-dbusmock / pip: python-dbusmock)",
                missing=[],
            ) from exc

        import os

        for spec in self.specs:
            bus_type = (
                dbusmock.BusType.SYSTEM if spec.bus == "system" else dbusmock.BusType.SESSION
            )
            # dbusmock 通过环境变量决定连哪条总线
            saved = {
                "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
                "DBUS_SYSTEM_BUS_ADDRESS": os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"),
            }
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = session_address
            if system_address:
                os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = system_address
            try:
                template = self._resolve_template(spec.template)
                log.debug("启动依赖 mock: %s (%s bus)", template, spec.bus)
                spawned = dbusmock.SpawnedMock.spawn_with_template(
                    template, dict(spec.params) or None, bus_type
                )
            except Exception as exc:
                self.stop()
                raise MockUnfaithful(
                    f"启动依赖 mock {spec.template!r} 失败: {exc}", missing=[]
                ) from exc
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self._running.append(_MockProcess(spec, spawned))

    def _resolve_template(self, name: str) -> str:
        """优先用框架自带模板,其次交给 dbusmock 解析内置模板名。"""
        local = TEMPLATE_DIR / f"{name}.py"
        if local.exists():
            return str(local)
        safe = name.replace(".", "_").replace("-", "_")
        local_safe = TEMPLATE_DIR / f"{safe}.py"
        if local_safe.exists():
            return str(local_safe)
        return name

    def check_faithful(self, service_output: str = "") -> None:
        """在被测进程失败后调用:给出精确缺失方法名。

        证据可能出现在两侧:mock 进程的输出,或被测服务自己打印的错误。
        """
        missing: list[str] = []
        for proc in self._running:
            for full in proc.missing_methods():
                if full not in missing:
                    missing.append(full)
        for full in scan_missing_methods(service_output):
            if full not in missing:
                missing.append(full)
        if missing:
            raise MockUnfaithful(
                "依赖 mock 缺少被测服务实际调用的方法", missing=missing
            )

    def set_state(self, template: str, method: str, args: tuple[Any, ...]) -> None:
        """mock-state 原语:向 mock 对象注入状态(调用其 org.freedesktop.DBus.Mock 接口)。"""
        for proc in self._running:
            if proc.spec.template == template or Path(proc.spec.template).stem == template:
                obj = getattr(proc.spawned, "obj", None)
                if obj is None:  # pragma: no cover - dbusmock 版本差异
                    raise MockUnfaithful(f"mock {template} 无可用对象句柄", missing=[])
                getattr(obj, method)(*args)
                return
        raise MockUnfaithful(
            f"mock-state 指定的模板 {template!r} 未在 needs: 中声明或未启动", missing=[]
        )

    def stop(self) -> None:
        for proc in self._running:
            proc.terminate()
        self._running.clear()
