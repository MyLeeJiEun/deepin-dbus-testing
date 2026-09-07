"""私有 DBus 总线管理.

实现要点(均有实测依据,见开发文档附录 A):
- 默认**不含标准 servicedir**:实测 dde-application-manager 在带标准 servicedir 的私有总线
  触发 org.freedesktop.systemd1 的 DBus 激活并失败。密闭测试必须禁止意外激活真实服务。
- **不使用 dbus-run-session 包裹**:实测其 teardown 在部分环境挂起。
- teardown 需降级:部分环境对 dbus-daemon 发信号返回 EPERM。
- 所有临时文件都在系统临时目录下(非侵入约束的单测会断言这一点)。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
from enum import Enum
from pathlib import Path
from types import TracebackType

from ..errors import BusError

log = logging.getLogger(__name__)

_DOCTYPE = (
    '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"\n'
    ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">'
)

_CONFIG_TEMPLATE = """{doctype}
<busconfig>
  <type>{bus_type}</type>
  <keep_umask/>
  <listen>unix:path={socket}</listen>
{servicedirs}
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

# 标准 session 服务目录(仅在 servicedirs=True 时加入)
_STANDARD_SESSION_DIRS = (
    "/usr/local/share/dbus-1/services",
    "/usr/share/dbus-1/services",
)
_STANDARD_SYSTEM_DIRS = (
    "/usr/local/share/dbus-1/system-services",
    "/usr/share/dbus-1/system-services",
)


class BusType(Enum):
    SESSION = "session"
    SYSTEM = "system"

    @property
    def env_name(self) -> str:
        return f"DBUS_{self.value.upper()}_BUS_ADDRESS"


class PrivateBus:
    """一条私有 dbus-daemon。start() 后可用 address / env。"""

    def __init__(self, bus_type: BusType = BusType.SESSION, *, servicedirs: bool = False) -> None:
        self.bus_type = bus_type
        self._servicedirs = servicedirs
        self._dir: Path | None = None
        self._address: str | None = None
        self._pid: int | None = None
        self._keep = False

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> str:
        if self._address is not None:
            return self._address
        if shutil.which("dbus-daemon") is None:
            raise BusError("找不到 dbus-daemon,请安装 dbus 包")

        self._dir = Path(tempfile.mkdtemp(prefix=f"dbus-testing-{self.bus_type.value}-"))
        socket = self._dir / "bus.socket"
        private_services = self._dir / "services"
        private_services.mkdir(parents=True, exist_ok=True)

        dirs = [private_services]
        if self._servicedirs:
            std = (
                _STANDARD_SESSION_DIRS
                if self.bus_type is BusType.SESSION
                else _STANDARD_SYSTEM_DIRS
            )
            dirs.extend(Path(p) for p in std)
        servicedir_xml = "\n".join(f"  <servicedir>{d}</servicedir>" for d in dirs)

        config = self._dir / "bus.conf"
        config.write_text(
            _CONFIG_TEMPLATE.format(
                doctype=_DOCTYPE,
                bus_type=self.bus_type.value,
                socket=socket,
                servicedirs=servicedir_xml,
            ),
            encoding="utf-8",
        )

        argv = [
            "dbus-daemon",
            f"--config-file={config}",
            "--print-address=1",
            "--print-pid=1",
            "--nopidfile",
            "--fork",
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - 固定 argv,无 shell
                argv, capture_output=True, timeout=15, check=False
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - 环境异常
            self._cleanup_dir()
            raise BusError("启动 dbus-daemon 超时") from exc

        out = proc.stdout.decode("utf-8", "replace").split()
        if proc.returncode != 0 or len(out) < 2:
            err = proc.stderr.decode("utf-8", "replace").strip()
            self._cleanup_dir()
            raise BusError(
                f"启动 dbus-daemon 失败(returncode={proc.returncode}): {err or '无输出'}"
            )

        self._address = out[0]
        try:
            self._pid = int(out[1])
        except ValueError:  # pragma: no cover - dbus-daemon 输出异常
            self._pid = None

        if not socket.exists():  # pragma: no cover - dbus-daemon 行为异常
            self.stop()
            raise BusError("dbus-daemon 未创建监听 socket")

        log.debug("私有 %s 总线已启动: %s (pid=%s)", self.bus_type.value, self._address, self._pid)
        return self._address

    def stop(self) -> None:
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # 部分环境对 dbus-daemon 有信号保护(实测 EPERM),降级放行。
                log.warning(
                    "无权限终止 dbus-daemon(pid=%s),交由系统回收;常规 CI 容器无此限制",
                    self._pid,
                )
            self._pid = None
        self._address = None
        if not self._keep:
            self._cleanup_dir()

    def keep(self) -> None:
        """--keep-bus:保留总线与临时目录。"""
        self._keep = True

    def _cleanup_dir(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # ------------------------------------------------------------------ 属性

    @property
    def address(self) -> str:
        if self._address is None:
            raise BusError("总线尚未启动,请先调用 start()")
        return self._address

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def workdir(self) -> Path | None:
        return self._dir

    @property
    def env(self) -> dict[str, str]:
        return {self.bus_type.env_name: self.address}

    # ------------------------------------------------------------------ 上下文

    def __enter__(self) -> PrivateBus:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def attach_bus_env(bus_type: BusType = BusType.SESSION) -> dict[str, str]:
    """attach 模式:复用外部(真实会话)总线地址。"""
    addr = os.environ.get(bus_type.env_name)
    if not addr:
        raise BusError(
            f"attach 模式需要 {bus_type.env_name},当前环境未设置"
            "(请在真实会话中运行,或改用 mode: isolate)"
        )
    return {bus_type.env_name: addr}
