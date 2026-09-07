"""被测服务的拉起、就绪判定与回收.

实现要点(均有实测依据,见开发文档附录 A):
- 二进制来自**构建产物**(binary.search + ${BUILD_DIR}),不依赖系统安装。
- **存活判定禁用 pgrep**(实测会匹配到桌面会话中同名进程,假阳性):
  只用 Popen 句柄 + name-owner 双判。
- 必须持续读取子进程输出,否则管道写满会阻塞子进程。
- go-loader 形态用 `--enable <module>`(实测 dde-session-daemon 支持)。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from ..errors import BinaryNotFound, ConfigError, ProcessDied, ReadyTimeout
from ..model import UNSET_MARKER, ServiceSpec
from .client import BusClient

log = logging.getLogger(__name__)

STDERR_BUFFER_LINES = 200
# 与 debian 打包路径一致(hosts/dsm-host 的 CMake 装到 libexec/dbus-testing/)
DSM_HOST_CANDIDATES = (
    "dbus-testing-dsm-host",  # PATH 中(pip/本地构建后手工放置)
    "/usr/libexec/dbus-testing/dbus-testing-dsm-host",
    "/usr/lib/dbus-testing/dbus-testing-dsm-host",
)


class ServiceHandle:
    """一个已拉起的被测进程。"""

    def __init__(
        self,
        spec: ServiceSpec,
        process: subprocess.Popen[bytes] | None,
        resolved_binary: Path | None,
        bus_address: str,
        argv: list[str],
    ) -> None:
        self.spec = spec
        self.process = process
        self.resolved_binary = resolved_binary
        self.bus_address = bus_address
        self.argv = argv
        self.services = spec.services
        self._lines: deque[str] = deque(maxlen=STDERR_BUFFER_LINES)
        self._reader: threading.Thread | None = None
        if process is not None and process.stdout is not None:
            self._reader = threading.Thread(target=self._drain, daemon=True)
            self._reader.start()

    def _drain(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            self._lines.append(raw.decode("utf-8", "replace").rstrip("\n"))

    def is_alive(self) -> bool:
        if self.process is None:  # attach 模式
            return True
        return self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def output_tail(self, n: int = 20) -> str:
        lines = list(self._lines)[-n:]
        return "\n".join(lines)

    def terminate(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            return
        sig = signal.SIGKILL if self.spec.teardown == "kill" else signal.SIGTERM
        try:
            os.killpg(os.getpgid(self.process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.process.send_signal(sig)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
            try:
                self.process.kill()
            except ProcessLookupError:
                pass


class Launcher:
    """按 kind 解析构建产物、拉起进程、等待就绪。"""

    def resolve_binary(self, spec: ServiceSpec) -> Path:
        candidates: list[tuple[str, str]] = []
        for raw in spec.binary_search:
            if UNSET_MARKER in raw:
                candidates.append((raw, "未提供 --build-dir,已跳过"))
                continue
            p = Path(raw)
            if p.exists() and p.is_file():
                if not os.access(p, os.X_OK):
                    candidates.append((raw, "存在但不可执行"))
                    continue
                log.debug("二进制解析到: %s", p)
                return p
            candidates.append((raw, "不存在" if not p.exists() else "不是普通文件"))
        detail = "\n".join(f"  - {path}  [{why}]" for path, why in candidates) or "  (未配置候选)"
        raise BinaryNotFound(
            f"未找到可执行的被测二进制。候选(binary.search)逐条判定:\n{detail}",
            candidates=candidates,
            build_dir=str(spec.build_dir) if spec.build_dir else None,
        )

    def _resolve_dsm_host(self) -> str:
        for cand in DSM_HOST_CANDIDATES:
            if "/" in cand:
                found = cand if Path(cand).exists() else None
            else:
                found = shutil.which(cand)
            if found:
                return found
        raise ConfigError(
            "kind=dsm 需要可选组件 dsm-host(包 dbus-testing-dsm-host)。"
            "未安装时请将该服务改为 mode: attach 仅做只读契约校验。"
        )

    def build_argv(self, spec: ServiceSpec, binary: Path | None, bus_address: str) -> list[str]:
        if spec.kind == "process":
            assert binary is not None
            return [str(binary), *spec.args]

        if spec.kind == "go-loader":
            assert binary is not None
            if not spec.module:
                raise ConfigError("kind=go-loader 必须提供 module")
            return [str(binary), "--enable", spec.module, "-i", *spec.args]

        if spec.kind == "dsm":
            if not spec.plugin:
                raise ConfigError("kind=dsm 必须提供 plugin(.so 路径)")
            host = self._resolve_dsm_host()
            return [
                host,
                "--plugin",
                spec.plugin,
                "--name",
                spec.primary_service,
                "--bus",
                bus_address,
                *spec.args,
            ]

        # plugin-host:宿主二进制 + -p 插件路径(宿主加载参数待逐仓实测)
        if not spec.plugin:
            raise ConfigError("kind=plugin-host 必须提供 plugin(.so 路径)")
        host_binary = spec.host_binary or (str(binary) if binary else None)
        if not host_binary:
            raise ConfigError(
                "kind=plugin-host 需要 host-binary 或 binary.search 指向插件宿主可执行文件"
            )
        return [host_binary, "-p", spec.plugin, *spec.args]

    def launch(
        self,
        spec: ServiceSpec,
        bus_address: str,
        env_overlay: dict[str, str],
    ) -> ServiceHandle:
        binary: Path | None = None
        if spec.kind in ("process", "go-loader") or spec.binary_search:
            binary = self.resolve_binary(spec)
        argv = self.build_argv(spec, binary, bus_address)

        env = dict(os.environ)
        env.update(env_overlay)

        log.debug("拉起被测进程: %s", " ".join(argv))
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv 来自配置,无 shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessDied(f"无法执行 {argv[0]}: {exc}", argv=argv) from exc
        return ServiceHandle(spec, proc, binary, bus_address, argv)

    def attach(self, spec: ServiceSpec, bus_address: str) -> ServiceHandle:
        """attach 模式:不拉起进程,只绑定到已运行的服务。"""
        return ServiceHandle(spec, None, None, bus_address, [])

    def wait_ready(self, handle: ServiceHandle, client: BusClient) -> None:
        spec = handle.spec
        expected = list(spec.ready.name_owner)
        deadline = time.monotonic() + spec.ready.timeout
        while True:
            if not handle.is_alive():
                raise ProcessDied(
                    f"被测进程在就绪前退出(exit={handle.exit_code})",
                    exit_code=handle.exit_code,
                    output=handle.output_tail(20),
                    argv=handle.argv,
                )
            missing = [n for n in expected if not client.name_has_owner(n)]
            if not missing:
                log.debug("服务已就绪: %s", ", ".join(expected))
                return
            if time.monotonic() >= deadline:
                raise ReadyTimeout(
                    f"等待服务注册超时({spec.ready.timeout}s):{', '.join(missing)} 未出现在总线上",
                    expected=expected,
                    missing=missing,
                    acquired=client.list_acquired_names(),
                    output=handle.output_tail(20),
                    alive=handle.is_alive(),
                )
            time.sleep(0.1)
