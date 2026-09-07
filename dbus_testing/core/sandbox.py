"""测试沙箱:隔离 HOME 与 XDG 目录,避免污染开发者环境。

XDG_DATA_DIRS 保持继承(服务需读系统 desktop 文件与 schema)。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from types import TracebackType

from ..model import SandboxSpec

log = logging.getLogger(__name__)


class Sandbox:
    """per-run(或 per-case)的环境隔离。__enter__ 返回要注入子进程的 env 增量。"""

    def __init__(self, spec: SandboxSpec) -> None:
        self.spec = spec
        self._dir: Path | None = None
        self._keep = False

    def __enter__(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.spec.home == "tmp":
            self._dir = Path(tempfile.mkdtemp(prefix="dbus-testing-home-"))
            data = self._dir / "share"
            config = self._dir / "config"
            cache = self._dir / "cache"
            runtime = self._dir / "runtime"
            for d in (data, config, cache, runtime):
                d.mkdir(parents=True, exist_ok=True)
            runtime.chmod(0o700)
            env.update(
                HOME=str(self._dir),
                XDG_DATA_HOME=str(data),
                XDG_CONFIG_HOME=str(config),
                XDG_CACHE_HOME=str(cache),
                XDG_RUNTIME_DIR=str(runtime),
            )
            log.debug("沙箱 HOME: %s", self._dir)
        # 显式 env 覆盖优先级最高(如 QT_QPA_PLATFORM=offscreen)
        env.update(self.spec.env)
        return env

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._dir is not None and not self._keep:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def keep(self) -> None:
        self._keep = True

    @property
    def home(self) -> Path | None:
        return self._dir
