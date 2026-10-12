"""pytest 插件:把 tests.yaml 编译成原生 pytest 用例,并为逃生舱提供 fixtures。

采用自定义 Collector(而非 pytest_generate_tests):天然支持 `tests.yaml:行号` 定位。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .core.diagnose import diagnose
from .engine import (
    SERVICE_FILENAME,
    TESTS_FILENAME,
    Engine,
    RunContext,
    SessionManager,
    load_baseline,
)
from .errors import DbusTestingError
from .model import Case, ServiceSpec, load_service
from .results import CaseResult

_MANAGERS: dict[Path, SessionManager] = {}


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
    _warn_if_plugin_not_installed(config)


def _warn_if_plugin_not_installed(config: pytest.Config) -> None:
    """插件被显式 -p 加载但未作为包安装时,提示逃生舱 fixture 的依赖关系。

    本模块通常经 pytest11 entry point 自动加载;若用户把 tests/dbus 拷到别的仓、
    或在子进程里 `python3 -m pytest`,而项目又没装上,fixture(dbus_service 等)
    就不会出现在 fixture 列表里,报错是 'fixture not found',与插件本身无关——
    这种"静默缺失"极难归因,故在此主动给出可操作提示。
    """
    try:
        from importlib.metadata import entry_points

        installed = any(
            ep.value == "dbus_testing.pytest_plugin"
            for ep in entry_points(group="pytest11")
        )
    except Exception:  # pragma: no cover - 元数据不可用的极端环境
        return
    if installed:
        return
    config.issue_config_time_warning(
        pytest.PytestConfigWarning(
            "dbus_testing 插件未被安装(无 pytest11 entry point),当前是显式 -p 加载。"
            "若逃生舱用例报 \"fixture 'dbus_service' not found\",请在本仓执行 "
            "`pip install -e .`(或给 pytest 加 -p dbus_testing.pytest_plugin)。"
        ),
        stacklevel=2,
    )


def _build_dir(config: pytest.Config) -> Path | None:
    raw = config.getoption("--dbus-build-dir")
    return Path(str(raw)).resolve() if raw else None


def _load_spec(service_yaml: Path, config: pytest.Config) -> ServiceSpec:
    return load_service(service_yaml, build_dir=_build_dir(config))


def _manager_for(service_yaml: Path, config: pytest.Config) -> SessionManager:
    """同一个 service.yaml 共用一个会话管理器;是否按用例重建由 isolation/restart 决定。"""
    key = service_yaml.resolve()
    if key in _MANAGERS:
        return _MANAGERS[key]
    spec = _load_spec(service_yaml, config)
    manager = SessionManager(
        spec,
        keep=bool(config.getoption("--dbus-keep-bus")),
        timeout_scale=float(config.getoption("--dbus-timeout-scale")),
        baseline=load_baseline(service_yaml.parent),
        tests_dir=service_yaml.parent,
    )
    manager.ensure()
    _MANAGERS[key] = manager
    return manager


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for manager in _MANAGERS.values():
        manager.close()
    _MANAGERS.clear()


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
            manager = _manager_for(service_yaml, self.config)
        except DbusTestingError as exc:
            raise CaseFailure(diagnose(exc).render()) from exc
        ctx = manager.context_for(self.case)
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


def _find_suite(start: Path) -> Path:
    """向上查找包含 service.yaml 的套件目录。"""
    for candidate in [start, *start.parents]:
        if (candidate / SERVICE_FILENAME).exists():
            return candidate
    raise pytest.UsageError(f"在 {start} 及其父目录中找不到 {SERVICE_FILENAME}")


@pytest.fixture
def dbus_tests_dir(request: pytest.FixtureRequest) -> Path:
    """当前测试文件所在的 tests/dbus 目录(需含 service.yaml)。

    function 作用域并用用例自身路径定位:session 级 fixture 的 request.node
    是 Session,路径是 rootdir,`pytest <目录>` 时会解析到错误位置(E1)。
    """
    node = request.node
    start = Path(str(getattr(node, "path", None) or node.fspath)).parent
    return _find_suite(start)


@pytest.fixture
def dbus_context(dbus_tests_dir: Path, pytestconfig: pytest.Config) -> RunContext:
    return _manager_for(dbus_tests_dir / SERVICE_FILENAME, pytestconfig).context


@pytest.fixture
def dbus_bus(dbus_tests_dir: Path, pytestconfig: pytest.Config) -> str:
    from .core.bus import BusType

    return _manager_for(dbus_tests_dir / SERVICE_FILENAME, pytestconfig).ensure().env[
        BusType.SESSION.env_name
    ]


class ServiceProxy:
    """逃生舱用的轻量代理:直接对被测服务发调用/读写属性/等信号。"""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self.spec = ctx.spec

    @property
    def client(self) -> Any:
        """始终返回当前会话的连接(isolation: per-case 下会话会被重建)。"""
        return self._ctx.client

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


@pytest.fixture
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
