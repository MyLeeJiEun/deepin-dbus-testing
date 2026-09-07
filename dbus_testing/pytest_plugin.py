"""pytest 插件:把 tests.yaml 编译成原生 pytest 用例,并为逃生舱提供 fixtures。

采用自定义 Collector(而非 pytest_generate_tests):天然支持 `tests.yaml:行号` 定位。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .core.diagnose import diagnose
from .core.guard import AttachGuard
from .engine import (
    SERVICE_FILENAME,
    TESTS_FILENAME,
    Engine,
    RunContext,
    Session,
    load_baseline,
)
from .errors import DbusTestingError
from .model import Case, ServiceSpec, load_service
from .results import CaseResult

_SESSIONS: dict[Path, tuple[Session, RunContext]] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("dbus-testing")
    group.addoption(
        "--dbus-build-dir",
        action="store",
        default=None,
        help="构建目录,供 service.yaml 的 ${BUILD_DIR} 展开",
    )
    group.addoption(
        "--dbus-keep-bus",
        action="store_true",
        default=False,
        help="跑完保留私有总线与沙箱供手工排障",
    )
    group.addoption(
        "--dbus-timeout-scale",
        action="store",
        type=float,
        default=1.0,
        help="统一放大所有超时(慢机器/CI)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "dbus: DBus 接口用例(由 tests.yaml 生成)")


def _build_dir(config: pytest.Config) -> Path | None:
    raw = config.getoption("--dbus-build-dir")
    return Path(str(raw)).resolve() if raw else None


def _load_spec(service_yaml: Path, config: pytest.Config) -> ServiceSpec:
    return load_service(service_yaml, build_dir=_build_dir(config))


def _session_for(service_yaml: Path, config: pytest.Config) -> tuple[Session, RunContext]:
    """同一个 service.yaml 共用一条会话(默认 per-run 总线粒度)。"""
    key = service_yaml.resolve()
    if key in _SESSIONS:
        return _SESSIONS[key]
    spec = _load_spec(service_yaml, config)
    keep = bool(config.getoption("--dbus-keep-bus"))
    session = Session(spec, keep=keep)
    session.start()
    assert session.client is not None
    ctx = RunContext(
        spec=spec,
        client=session.client,
        guard=AttachGuard(spec),
        mocks=session.mocks,
        baseline=load_baseline(service_yaml.parent),
        ignore=spec.ignore,
        tests_dir=service_yaml.parent,
        timeout_scale=float(config.getoption("--dbus-timeout-scale")),
        handle=session.handle,
    )
    _SESSIONS[key] = (session, ctx)
    return session, ctx


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for sess, _ctx in _SESSIONS.values():
        sess.stop()
    _SESSIONS.clear()


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    if file_path.name != TESTS_FILENAME:
        return None
    if not (file_path.parent / SERVICE_FILENAME).exists():
        return None
    return YamlFile.from_parent(parent, path=file_path)


class YamlFile(pytest.File):
    """一份 tests.yaml → 一批 pytest 用例。"""

    def collect(self) -> Iterator[pytest.Item]:
        service_yaml = self.path.parent / SERVICE_FILENAME
        spec = _load_spec(service_yaml, self.config)
        for case in Engine().compile(self.path, spec):
            yield YamlCase.from_parent(self, name=case.name, case=case)


class CaseFailure(Exception):
    """用例失败(已带诊断文本)。"""


class YamlCase(pytest.Item):
    def __init__(self, *, case: Case, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.case = case
        self.add_marker(pytest.mark.dbus)
        self._result: CaseResult | None = None

    def runtest(self) -> None:
        service_yaml = self.path.parent / SERVICE_FILENAME
        try:
            _session, ctx = _session_for(service_yaml, self.config)
        except DbusTestingError as exc:
            raise CaseFailure(diagnose(exc).render()) from exc
        result = Engine().run_case(self.case, ctx)
        self._result = result
        if result.status == "skip":
            pytest.skip(result.summary or "用例声明跳过")
        if result.status == "pass":
            return
        parts = [f"[{result.code or 'E_ASSERT'}] {result.summary}"]
        for step in result.steps:
            mark = "ok" if step.ok else "FAILED"
            parts.append(f"  - {step.op}: {mark} {step.detail}")
        if result.detail:
            parts.append(result.detail)
        if result.hint:
            parts.append(f"提示: {result.hint}")
        raise CaseFailure("\n".join(parts))

    def repr_failure(self, excinfo: pytest.ExceptionInfo[BaseException], style: Any = None) -> str:
        if isinstance(excinfo.value, CaseFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style)  # type: ignore[return-value]

    def reportinfo(self) -> tuple[Path, int, str]:
        line = max(0, self.case.line - 1)
        return self.path, line, self.name


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def dbus_tests_dir(request: pytest.FixtureRequest) -> Path:
    """当前测试文件所在的 tests/dbus 目录(需含 service.yaml)。"""
    start = Path(str(request.node.fspath)).parent
    for candidate in [start, *start.parents]:
        if (candidate / SERVICE_FILENAME).exists():
            return candidate
    raise pytest.UsageError(f"在 {start} 及其父目录中找不到 {SERVICE_FILENAME}")


@pytest.fixture(scope="session")
def dbus_context(dbus_tests_dir: Path, pytestconfig: pytest.Config) -> RunContext:
    _session, ctx = _session_for(dbus_tests_dir / SERVICE_FILENAME, pytestconfig)
    return ctx


@pytest.fixture(scope="session")
def dbus_bus(dbus_tests_dir: Path, pytestconfig: pytest.Config) -> str:
    session, _ctx = _session_for(dbus_tests_dir / SERVICE_FILENAME, pytestconfig)
    from .core.bus import BusType

    return session.env[BusType.SESSION.env_name]


class ServiceProxy:
    """逃生舱用的轻量代理:直接对被测服务发调用/读写属性/等信号。"""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self.spec = ctx.spec
        self.client = ctx.client

    def _target(self, member: str, *, interface: str | None = None, path: str | None = None) -> Any:
        from .model import Target

        return Target(
            service=self.spec.primary_service,
            path=path or self.spec.default_path,
            interface=interface or self.spec.primary_service,
            member=member,
        )

    def call(self, method: str, *args: Any, interface: str | None = None,
             path: str | None = None, timeout: float = 5.0) -> Any:
        out = self.client.call(self._target(method, interface=interface, path=path), args, timeout)
        if not out.ok:
            raise AssertionError(f"{method} 调用失败: {out.error_name}: {out.error_message}")
        return out.value

    def signature(self, method: str, *args: Any, **kw: Any) -> str:
        out = self.client.call(self._target(method, **kw), args)
        return out.signature

    def get(self, prop: str, **kw: Any) -> Any:
        out = self.client.get_prop(self._target(prop, **kw))
        if not out.ok:
            raise AssertionError(f"读属性 {prop} 失败: {out.error_name}: {out.error_message}")
        return out.value

    def set(self, prop: str, value: Any, **kw: Any) -> None:
        out = self.client.set_prop(self._target(prop, **kw), value)
        if not out.ok:
            raise AssertionError(f"写属性 {prop} 失败: {out.error_name}: {out.error_message}")


@pytest.fixture(scope="session")
def dbus_service(dbus_context: RunContext) -> ServiceProxy:
    return ServiceProxy(dbus_context)


@pytest.fixture
def signal_wait(dbus_context: RunContext) -> Any:
    """用法:with signal_wait(service, "Changed", timeout=5): service.call("DoIt")"""
    from contextlib import contextmanager

    from .core.client import wait_until
    from .model import Target

    @contextmanager
    def _wait(
        proxy: ServiceProxy,
        signal: str,
        *,
        interface: str | None = None,
        path: str | None = None,
        timeout: float = 5.0,
    ) -> Iterator[list[Any]]:
        spec = proxy.spec
        target = Target(
            service=spec.primary_service,
            path=path or spec.default_path,
            interface=interface or spec.primary_service,
            member=signal,
        )
        with dbus_context.client.capture(target) as received:
            yield received
            if not wait_until(lambda: bool(received), timeout * dbus_context.timeout_scale):
                raise AssertionError(f"等待信号 {signal} 超时({timeout}s)")

    return _wait
