"""用例编译与执行、会话生命周期、覆盖矩阵计算。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from .core.bus import BusType, PrivateBus, attach_bus_env
from .core.client import BusClient, wait_until
from .core.guard import AttachGuard
from .core.launcher import Launcher, ServiceHandle
from .core.mockdeps import MockDeps
from .core.sandbox import Sandbox
from .errors import (
    AssertionFailed,
    ConfigError,
    ContractDrift,
    DbusTestingError,
    ProcessDied,
    ReadyTimeout,
    SignalTimeout,
)
from .model import UNSET, Case, IgnoreSpec, ServiceSpec, Step, load_cases
from .results import (
    CallOutcome,
    CaseResult,
    CoverageMatrix,
    DeclaredStatus,
    Delta,
    MemberCoverage,
    RunResult,
    StepResult,
)
from .scanner.diff import diff
from .scanner.introspect import snapshot
from .scanner.normalize import normalize_tree, parse_members
from .scanner.srcxml import src_baseline_xml, static_diff

log = logging.getLogger(__name__)

CONTRACT_FILENAME = "contract.xml"
# 常见的 polkit 拒绝错误名(不同服务实现措辞不一)
POLKIT_DENIAL_HINTS = (
    "PolicyKit1",
    "NotAuthorized",
    "AuthFailed",
    "AccessDenied",
    "Unauthorized",
)


def is_polkit_denial(error_name: str | None) -> bool:
    return bool(error_name) and any(k in str(error_name) for k in POLKIT_DENIAL_HINTS)
TESTS_FILENAME = "tests.yaml"
SERVICE_FILENAME = "service.yaml"


# --------------------------------------------------------------------------- 会话


class Session:
    """一次运行的完整环境:私有总线(+system)→ 依赖 mock → 沙箱 → 被测进程 → 客户端。

    执行顺序不可颠倒(实测:依赖缺失会让被测服务直接 abort)。
    """

    def __init__(self, spec: ServiceSpec, *, keep: bool = False) -> None:
        self.spec = spec
        self.keep = keep
        self.bus: PrivateBus | None = None
        self.system_bus: PrivateBus | None = None
        self.sandbox: Sandbox | None = None
        self.mocks = MockDeps(spec.needs)
        self.launcher = Launcher()
        self.handle: ServiceHandle | None = None
        self.client: BusClient | None = None
        self.env: dict[str, str] = {}
        # restart_service 复用:主交互总线地址与注入被测进程的环境增量
        self._address: str | None = None
        self._overlay: dict[str, str] = {}

    def __enter__(self) -> Session:
        try:
            self.start()
        except BaseException:
            # __enter__ 抛异常时 Python 不会调 __exit__,半成品(总线/mock/沙箱)
            # 必须在这里自己回收;keep=True 时保留现场供 --keep-bus 排障。
            self.stop()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        spec = self.spec
        bus_env: dict[str, str] = {}
        session_address: str = ""
        # 被测服务的主交互总线:system-bus 服务注册在 system bus 上,
        # 测试客户端(就绪探测、用例调用)也必须走 system bus,否则永远等不到就绪。
        if spec.mode == "attach":
            bus_env = attach_bus_env(BusType.SESSION)
            address = bus_env[BusType.SESSION.env_name]
            if spec.system_bus:
                # attach 也认 system-bus: true(R3):主交互总线切到真实系统总线,
                # 客户端/快照/契约校验都走它;环境没设地址时 fail-fast,
                # 绝不静默错总线(修前会连 session 总线然后报 0 对象、退出码 0)。
                bus_env.update(attach_bus_env(BusType.SYSTEM))
                address = bus_env[BusType.SYSTEM.env_name]
        else:
            self.bus = PrivateBus(BusType.SESSION)
            session_address = self.bus.start()
            address = session_address
            bus_env.update(self.bus.env)
            if self.keep:
                self.bus.keep()
            if spec.system_bus:
                self.system_bus = PrivateBus(BusType.SYSTEM)
                self.system_bus.start()
                bus_env.update(self.system_bus.env)
                if self.keep:
                    self.system_bus.keep()
                address = self.system_bus.address

        self.env = dict(bus_env)
        self.client = BusClient(address)

        if spec.mode == "attach":
            self.handle = self.launcher.attach(spec, address)
            return

        if any(m.bus == "system" for m in spec.needs) and self.system_bus is None:
            raise ConfigError(
                f"{spec.source}: needs 里有 bus: system 的 mock,"
                f"请在 service.yaml 加 system-bus: true,否则 mock 会回退到真实 system bus"
            )

        if spec.has_polkit_rules and not any(
            "polkit" in m.template.lower() for m in spec.needs
        ):
            log.warning(
                "service.yaml 声明了 auth.polkit(受门禁的方法),但 needs: 里没有 polkitd mock;"
                "这些方法在密闭环境里会返回未授权错误。如需放行请加 `- mock: polkitd`"
            )

        # 1) 依赖 mock 必须先于被测进程
        import os

        saved = {k: os.environ.get(k) for k in bus_env}
        os.environ.update(bus_env)
        try:
            self.mocks.start(
                session_address,
                self.system_bus.address if self.system_bus is not None else None,
            )
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        # 2) 沙箱
        self.sandbox = Sandbox(spec.sandbox)
        sandbox_env = self.sandbox.__enter__()
        if self.keep:
            self.sandbox.keep()
        self.env.update(sandbox_env)

        overlay = dict(bus_env)
        overlay.update(sandbox_env)
        self._address = address
        self._overlay = overlay

        # 3) 拉起被测进程并等待就绪
        self._launch()

    def _launch(self) -> None:
        """拉起被测进程并等待就绪(restart_service 复用同一套总线/mock/沙箱)。"""
        assert self._address is not None and self.client is not None
        self.handle = self.launcher.launch(self.spec, self._address, self._overlay)
        try:
            self.launcher.wait_ready(self.handle, self.client)
        except (ProcessDied, ReadyTimeout):
            # 依赖 mock 不保真是最常见根因,优先给出精确缺失方法名
            if self.mocks.active:
                self.mocks.check_faithful(self.handle.output_tail(200))
            raise

    def restart_service(self) -> None:
        """重启被测进程,总线/mock/沙箱原样复用(service.yaml 的 restart: per-case)。"""
        if self.handle is None:
            raise ConfigError(
                f"{self.spec.source}: attach 模式下服务不由框架拉起,无法重启;"
                f"请去掉 restart 或改用 mode: isolate"
            )
        self.handle.terminate()
        self.handle = None
        self._launch()

    def stop(self, *, force: bool = False) -> None:
        """回收会话;force=True 时忽略 keep(per-case 重建中间会话时用)。"""
        if force:
            if self.sandbox is not None:
                self.sandbox.discard()
            for bus in (self.system_bus, self.bus):
                if bus is not None:
                    bus.discard()
        if self.handle is not None and (force or not self.keep):
            self.handle.terminate()
        if self.client is not None and (force or not self.keep):
            self.client.close()
        if force or not self.keep:
            self.mocks.stop()
            if self.sandbox is not None:
                self.sandbox.__exit__(None, None, None)
            if self.system_bus is not None:
                self.system_bus.stop()
            if self.bus is not None:
                self.bus.stop()

    def env_fingerprint(self) -> dict[str, str]:
        spec = self.spec
        out = {
            "service": spec.primary_service,
            "services": ", ".join(spec.services),
            "mode": spec.mode,
            "kind": spec.kind,
            "config": str(spec.source),
            "build_dir": str(spec.build_dir) if spec.build_dir else "(未指定)",
            "bus_address": self.env.get(BusType.SESSION.env_name, "(未知)"),
        }
        if spec.tier:
            out["tier"] = spec.tier
        if self.handle is not None and self.handle.resolved_binary is not None:
            out["binary"] = str(self.handle.resolved_binary)
        if self.handle is not None and self.handle.argv:
            out["argv"] = " ".join(self.handle.argv)
        if self.system_bus is not None:
            out["system_bus_address"] = self.system_bus.address
        if self.sandbox is not None and self.sandbox.home is not None:
            out["sandbox_home"] = str(self.sandbox.home)
        if spec.needs:
            out["mocks"] = ", ".join(m.template for m in spec.needs)
        return out


# --------------------------------------------------------------------------- 执行上下文


@dataclass
class RunContext:
    spec: ServiceSpec
    manager: SessionManager
    guard: AttachGuard
    baseline: str
    ignore: IgnoreSpec
    tests_dir: Path
    timeout_scale: float = 1.0
    live_xml: str = ""
    contract_deltas: list[Delta] = field(default_factory=list)

    # client / mocks / handle 随 isolation: per-case 的会话重建而变化,
    # 统一经 manager 取,任何持有者都不会拿到已关闭的旧连接。
    @property
    def client(self) -> BusClient:
        assert self.manager.session is not None and self.manager.session.client is not None
        return self.manager.session.client

    @property
    def mocks(self) -> MockDeps:
        assert self.manager.session is not None
        return self.manager.session.mocks

    @property
    def handle(self) -> ServiceHandle | None:
        return self.manager.session.handle if self.manager.session is not None else None


class SessionManager:
    """按 isolation / restart 语义提供执行环境,并持有唯一的 RunContext。

    - `isolation: per-run`(默认)整套用例共享一个会话;
    - `isolation: per-case`       每条用例重建会话(总线 + mock + 沙箱 + 进程);
    - `restart:   per-case`       复用会话,只重启被测进程。

    `isolation: per-case` 已隐含重启进程,此时 `restart` 不再产生额外动作。
    """

    def __init__(
        self,
        spec: ServiceSpec,
        *,
        keep: bool = False,
        timeout_scale: float = 1.0,
        baseline: str = "",
        tests_dir: Path = Path("."),
    ) -> None:
        self.spec = spec
        self.keep = keep
        self.session: Session | None = None
        self.context = RunContext(
            spec=spec,
            manager=self,
            guard=AttachGuard(spec),
            baseline=baseline,
            ignore=spec.ignore,
            tests_dir=tests_dir,
            timeout_scale=timeout_scale,
        )
        self._case: Case | None = None

    # ------------------------------------------------------------------ 生命周期

    def __enter__(self) -> SessionManager:
        self.ensure()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def ensure(self) -> Session:
        if self.session is None:
            session = Session(self.spec, keep=self.keep)
            try:
                session.start()
            except BaseException:
                # 半成品自己回收;keep=True 时保留现场供排障
                session.stop()
                raise
            self.session = session
        return self.session

    def context_for(self, case: Case) -> RunContext:
        """取该用例的执行环境,按 isolation / restart 决定是否重建。"""
        self.ensure()
        if self._case is not None and self._case is not case:
            if self.spec.isolation == "per-case":
                self._recycle()
            elif self.spec.restart == "per-case":
                assert self.session is not None
                self.session.restart_service()
        self._case = case
        return self.context

    def _recycle(self) -> None:
        """丢弃当前会话并重建(per-case):旧会话必须真回收,不留孤儿总线。"""
        assert self.session is not None
        self.session.stop(force=True)
        self.session = Session(self.spec, keep=self.keep)
        self.session.start()

    def close(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None

    def env_fingerprint(self) -> dict[str, str]:
        return self.session.env_fingerprint() if self.session is not None else {}

    @property
    def client(self) -> BusClient:
        assert self.session is not None and self.session.client is not None
        return self.session.client

    @property
    def mocks(self) -> MockDeps:
        assert self.session is not None
        return self.session.mocks


# --------------------------------------------------------------------------- 断言


def _fmt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 300 else text[:297] + "..."


def evaluate(step: Step, outcome: CallOutcome) -> tuple[bool, str]:
    """断言求值。顺序固定:error → (期望出错则短路)→ signature → type → value → nonempty。"""
    exp = step.expect

    # 1) error 维度
    if exp.expects_error:
        if outcome.ok:
            return False, f"期望出错但调用成功(返回 {_fmt(outcome.value)})"
        if exp.error != "*" and outcome.error_name != exp.error:
            return False, f"期望错误 {exp.error},实际 {outcome.error_name}: {outcome.error_message}"
        return True, f"按预期返回错误 {outcome.error_name}"
    if not outcome.ok:
        return False, f"调用失败: {outcome.error_name}: {outcome.error_message}"

    checks: list[str] = []

    # 3) signature
    if exp.signature is not None:
        if outcome.signature != exp.signature:
            return False, f"签名不符:期望 {exp.signature!r},实际 {outcome.signature!r}"
        checks.append(f"signature={exp.signature}")

    # 4) type(单返回值的类型码)
    if exp.type is not None:
        if outcome.signature != exp.type:
            return False, f"类型不符:期望 {exp.type!r},实际 {outcome.signature!r}"
        checks.append(f"type={exp.type}")

    # 5) value
    if exp.value is not UNSET:
        expected = exp.value
        actual = outcome.value
        if _normalize_for_compare(expected) != _normalize_for_compare(actual):
            return False, f"返回值不符:期望 {_fmt(expected)},实际 {_fmt(actual)}"
        checks.append("value 匹配")

    # 6) nonempty
    if exp.nonempty:
        value = outcome.value
        empty = value is None or (hasattr(value, "__len__") and len(value) == 0) or value == 0
        if empty:
            return False, f"期望非空,实际 {_fmt(value)}"
        checks.append("非空")

    if not checks:
        checks.append("调用成功")
    return True, ";".join(checks)


def _normalize_for_compare(value: Any) -> Any:
    """比较前统一容器类型(YAML 只有 list,DBus 结构体是 tuple)。"""
    if isinstance(value, tuple):
        return [_normalize_for_compare(v) for v in value]
    if isinstance(value, list):
        return [_normalize_for_compare(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_for_compare(v) for k, v in value.items()}
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------- 引擎


class Engine:
    """编译 tests.yaml,并逐用例执行。"""

    def compile(self, tests_path: Path, spec: ServiceSpec) -> list[Case]:
        return load_cases(tests_path, spec)

    # -------------------------------------------------------------- 单步执行

    def _timeout(self, step: Step, ctx: RunContext, default: float) -> float:
        base = step.timeout if step.timeout is not None else default
        return base * ctx.timeout_scale

    def run_step(
        self,
        step: Step,
        ctx: RunContext,
        captures: dict[int, list[Any]],
    ) -> StepResult:
        started = time.monotonic()
        ctx.guard.check(step)

        if step.op == "call":
            assert step.target is not None
            outcome = ctx.client.call(step.target, step.args, self._timeout(step, ctx, 5.0))
        elif step.op == "get-prop":
            assert step.target is not None
            outcome = ctx.client.get_prop(step.target, self._timeout(step, ctx, 5.0))
        elif step.op == "set-prop":
            assert step.target is not None
            outcome = ctx.client.set_prop(
                step.target, step.value, self._timeout(step, ctx, 5.0)
            )
        elif step.op == "wait-signal":
            outcome = self._wait_signal(step, ctx, captures)
        elif step.op == "mock-state":
            assert step.mock_template and step.mock_method
            ctx.mocks.set_state(step.mock_template, step.mock_method, step.args)
            outcome = CallOutcome(ok=True, value=None, signature="")
        elif step.op == "check-contract":
            outcome = self._check_contract(ctx)
        else:  # pragma: no cover - model 已校验
            raise AssertionFailed(f"未知原语 {step.op}")

        ok, detail = evaluate(step, outcome)
        # auth.polkit 声明过的方法遇到未授权错误时,补一条门禁专属说明
        if step.target is not None and is_polkit_denial(outcome.error_name):
            gated = ctx.spec.is_polkit_gated(step.target.interface, step.target.member)
            if gated:
                note = (
                    f"(该方法在 service.yaml 的 auth.polkit 中声明受 polkit 门禁,"
                    f"实际返回 {outcome.error_name})"
                )
                detail = f"{detail} {note}" if ok else (
                    f"{detail} {note};密闭环境下需在 needs: 里加 `- mock: polkitd`,"
                    f"或把用例写成 expect: {{error: {outcome.error_name}}}"
                )
        return StepResult(
            step=step,
            ok=ok,
            detail=detail,
            outcome=outcome,
            elapsed=time.monotonic() - started,
        )

    def _wait_signal(
        self, step: Step, ctx: RunContext, captures: dict[int, list[Any]]
    ) -> CallOutcome:
        assert step.target is not None
        received = captures.get(id(step))
        if received is None:  # pragma: no cover - 未布扣(理论不可达)
            raise SignalTimeout(
                f"信号 {step.target.member} 未布扣", signal=step.target.member, received=[]
            )
        index = step.index
        timeout = self._timeout(step, ctx, 5.0)
        got = wait_until(lambda: len(received) > index, timeout)
        if not got:
            raise SignalTimeout(
                f"等待信号 {step.target.member} 第 {index + 1} 次发射超时({timeout}s),"
                f"实收 {len(received)} 条",
                signal=step.target.member,
                received=list(received),
            )
        payload = received[index]
        return CallOutcome(ok=True, value=payload, signature="")

    def _check_contract(self, ctx: RunContext) -> CallOutcome:
        tree = snapshot(ctx.client, ctx.spec.services, ctx.ignore)
        actual = normalize_tree(tree, ctx.ignore)
        ctx.live_xml = actual
        deltas = diff(ctx.baseline, actual) if ctx.baseline else []
        ctx.contract_deltas = list(deltas)
        if deltas:
            raise ContractDrift(
                f"运行时接口与 contract.xml 有 {len(deltas)} 处差异", deltas=deltas
            )
        return CallOutcome(ok=True, value=len(tree), signature="")

    # -------------------------------------------------------------- 用例执行

    def run_case(self, case: Case, ctx: RunContext) -> CaseResult:
        from .core.diagnose import diagnose

        if case.skip:
            return CaseResult(case=case, status="skip", summary=case.skip)

        attempts = 0
        last: CaseResult | None = None
        max_attempts = max(1, case.flaky + 1)
        while attempts < max_attempts:
            attempts += 1
            started = time.monotonic()
            steps: list[StepResult] = []
            # 所有 wait-signal 在用例开始前统一布扣(必须先于触发动作)
            from contextlib import ExitStack

            with ExitStack() as stack:
                captures: dict[int, list[Any]] = {}
                for step in case.steps:
                    if step.op == "wait-signal" and step.target is not None:
                        captures[id(step)] = stack.enter_context(ctx.client.capture(step.target))
                try:
                    for step in case.steps:
                        result = self.run_step(step, ctx, captures)
                        steps.append(result)
                        if not result.ok:
                            last = CaseResult(
                                case=case,
                                status="fail",
                                steps=steps,
                                elapsed=time.monotonic() - started,
                                attempts=attempts,
                                code="E_ASSERT",
                                summary=f"步骤 {step.op} 断言失败",
                                detail=result.detail,
                            )
                            break
                    else:
                        return CaseResult(
                            case=case,
                            status="pass",
                            steps=steps,
                            elapsed=time.monotonic() - started,
                            attempts=attempts,
                        )
                except DbusTestingError as exc:
                    d = diagnose(exc)
                    config_codes = ("E_CONFIG_INVALID", "E_GUARD_DENIED")
                    status = "config-error" if exc.code in config_codes else "fail"
                    if exc.code in ("E_PROC_DIED", "E_READY_TIMEOUT", "E_BUS", "E_MOCK_UNFAITHFUL"):
                        status = "error"
                    last = CaseResult(
                        case=case,
                        status=status,  # type: ignore[arg-type]
                        steps=steps,
                        elapsed=time.monotonic() - started,
                        attempts=attempts,
                        code=d.code,
                        summary=d.summary,
                        detail=d.detail,
                        hint=d.hint,
                    )
                    if status != "fail":
                        return last
        assert last is not None
        return last

    # -------------------------------------------------------------- 全量执行

    def run(self, cases: list[Case], ctx: RunContext) -> RunResult:
        started = time.monotonic()
        result = RunResult()
        for case in cases:
            # 每条用例先取环境:isolation/restart 为 per-case 时会在此重建会话
            ctx = ctx.manager.context_for(case)
            result.cases.append(self.run_case(case, ctx))
            for delta in ctx.contract_deltas:
                if delta not in result.contract:
                    result.contract.append(delta)
            if ctx.live_xml:
                result.live_xml = ctx.live_xml
        result.elapsed = time.monotonic() - started
        return result


# --------------------------------------------------------------------------- 覆盖矩阵


def _member_kind_order(kind: str) -> int:
    return {"method": 0, "signal": 1, "property": 2}.get(kind, 3)


def build_coverage(
    baseline_xml: str,
    runtime_xml: str,
    src_xml: str | None,
    cases: list[Case],
    results: list[CaseResult] | None = None,
) -> CoverageMatrix:
    """声明覆盖(源码/基线/运行时三方一致性)+ 执行覆盖(被哪些用例成功调用过)。

    `results` 提供执行结果时,执行覆盖只统计 `ok` 的步骤——失败与跳过的调用
    不能给覆盖率凑数(R2);未提供时(check 路径)不产出执行覆盖。
    """
    matrix = CoverageMatrix()
    base = parse_members(baseline_xml) if baseline_xml else {}
    live = parse_members(runtime_xml) if runtime_xml else {}
    src = parse_members(src_xml) if src_xml else None

    interfaces = set(base) | set(live) | set(src or {})
    for interface in sorted(interfaces):
        b = base.get(interface, {})
        r = live.get(interface, {})
        s = (src or {}).get(interface, {}) if src is not None else {}
        for member in sorted(set(b) | set(r) | set(s)):
            kind = (
                b.get(member, r.get(member, s.get(member, ("method", ""))))[0]
                if (member in b or member in r or member in s)
                else "method"
            )
            declared = _declared_status(
                member in s if src is not None else None,
                member in b,
                member in r,
                b.get(member, (None, None))[1],
                r.get(member, (None, None))[1],
                s.get(member, (None, None))[1] if src is not None else None,
                has_src=src is not None,
            )
            matrix.add(
                MemberCoverage(
                    interface=interface,
                    member=member,
                    kind=kind,  # type: ignore[arg-type]
                    declared=declared,
                )
            )

    # 执行覆盖:按 Target 反查。矩阵条目的建立仍看全部用例(报告里要能看到
    # "配置了但没成功执行"的成员),但 executed_by 只统计 ok 的步骤。
    ok_targets: dict[tuple[str, str], list[str]] = {}
    if results is not None:
        for case_result in results:
            for step_result in case_result.steps:
                target = step_result.step.target
                if step_result.ok and target is not None:
                    names = ok_targets.setdefault(
                        (target.interface, target.member), []
                    )
                    if case_result.name not in names:
                        names.append(case_result.name)

    for case in cases:
        for step in case.steps:
            if step.target is None:
                continue
            cov = matrix.get(step.target.interface, step.target.member)
            if cov is None:
                op_kind = {
                    "call": "method",
                    "get-prop": "property",
                    "set-prop": "property",
                    "wait-signal": "signal",
                }.get(step.op)
                if op_kind is None:
                    continue
                cov = MemberCoverage(
                    interface=step.target.interface,
                    member=step.target.member,
                    kind=op_kind,  # type: ignore[arg-type]
                    declared="unknown",
                )
                matrix.add(cov)
            ok_names = ok_targets.get((step.target.interface, step.target.member), [])
            if case.name in ok_names and case.name not in cov.executed_by:
                cov.executed_by.append(case.name)
    return matrix


def _declared_status(
    in_src: bool | None,
    in_base: bool,
    in_live: bool,
    base_sig: str | None,
    live_sig: str | None,
    src_sig: str | None,
    *,
    has_src: bool,
) -> DeclaredStatus:
    if in_base and in_live and base_sig != live_sig:
        return "mismatch"
    if has_src and in_src and in_base and src_sig != base_sig:
        return "mismatch"
    if in_base and in_live:
        if not has_src:
            # 仓内没有源码声明 XML 可比时,基线↔运行时一致即视为声明覆盖达成;
            # "无源码可比" 这一事实由 RunResult.static_skipped 在套件层单独报告。
            return "ok"
        return "ok" if in_src else "only-baseline"
    if in_live and not in_base:
        return "only-runtime"
    if in_base and not in_live:
        return "only-baseline"
    if has_src and in_src and not in_base and not in_live:
        return "only-src"
    return "unknown"


# --------------------------------------------------------------------------- 顶层入口


def load_baseline(tests_dir: Path) -> str:
    path = Path(tests_dir) / CONTRACT_FILENAME
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@dataclass
class SuiteOutcome:
    result: RunResult
    spec: ServiceSpec
    cases: list[Case]


def run_suite(
    tests_dir: Path,
    *,
    build_dir: Path | None = None,
    keep: bool = False,
    timeout_scale: float = 1.0,
    case_filter: str | None = None,
    include_static: bool = True,
    static_strict: bool = False,
) -> SuiteOutcome:
    """CLI `run` 的实现:建会话 → 编译用例 → 执行 → 汇总覆盖矩阵。"""
    from .model import load_service

    tests_dir = Path(tests_dir)
    spec = load_service(tests_dir / SERVICE_FILENAME, build_dir=build_dir)
    engine = Engine()
    cases = engine.compile(tests_dir / TESTS_FILENAME, spec)
    if case_filter:
        cases = [c for c in cases if case_filter in c.name]

    baseline = load_baseline(tests_dir)
    src_xml = (
        src_baseline_xml(spec.repo_root, spec.src_xml_globs, spec.ignore)
        if include_static
        else None
    )

    with SessionManager(
        spec, keep=keep, timeout_scale=timeout_scale, baseline=baseline, tests_dir=tests_dir
    ) as manager:
        result = engine.run(cases, manager.context)
        if not result.live_xml:
            # 补一次快照供覆盖矩阵使用;服务此时可能已被前面的用例弄成不可用
            # (例如刚测过一个会阻塞的方法),快照失败不应把整轮判为错误。
            try:
                tree = snapshot(manager.client, spec.services, spec.ignore)
                result.live_xml = normalize_tree(tree, spec.ignore)
            except DbusTestingError as exc:
                log.warning("收尾快照失败(不影响用例结果): %s", exc)
        result.env = manager.env_fingerprint()

    if include_static:
        if src_xml is None:
            result.static_skipped = "仓内未找到源码声明 XML(src-xml-globs 无命中)"
            if static_strict:
                # --strict:SKIP 不再静默放行(R3 系列审阅)——门禁空转必须显式失败
                result.cases.append(
                    synthetic_case(
                        "contract-static",
                        "fail",
                        code="E_CONFIG_INVALID",
                        summary="静态契约校验被跳过且启用了 --strict",
                        detail=result.static_skipped,
                        hint="在 service.yaml 补 src-xml-globs 指向源码声明 XML,或去掉 --strict",
                    )
                )
        elif baseline:
            result.static_contract = static_diff(src_xml, baseline)
            if result.static_contract:
                # 漂移必须置失败,不能只写进报告(R7):退出码是 CI 门禁唯一认的信号
                result.cases.append(
                    synthetic_case(
                        "contract-static",
                        "fail",
                        code="E_CONTRACT_DRIFT",
                        summary=f"源码声明与 contract.xml 有 {len(result.static_contract)} 处差异",
                        detail="\n".join(f"  {d.render()}" for d in result.static_contract),
                        hint="接口有意变更 → 更新 contract.xml;否则检查源码声明 XML 是否漏改",
                    )
                )
                log.warning(
                    "静态契约校验发现 %d 处漂移,已计为失败(与 check --static 同语义)",
                    len(result.static_contract),
                )

    result.coverage = build_coverage(baseline, result.live_xml, src_xml, cases, result.cases)
    return SuiteOutcome(result=result, spec=spec, cases=cases)


def synthetic_case(
    name: str,
    status: str,
    *,
    code: str = "",
    summary: str = "",
    detail: str = "",
    hint: str = "",
) -> CaseResult:
    """为无用例但需在 CI 里可见的检查(如契约静态比对)合成一个 testcase。"""
    case = Case(name=name, steps=())
    return CaseResult(
        case=case,
        status=status,  # type: ignore[arg-type]
        code=code or None,
        summary=summary,
        detail=detail,
        hint=hint,
    )
