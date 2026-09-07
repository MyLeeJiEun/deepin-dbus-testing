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
    ContractDrift,
    DbusTestingError,
    ProcessDied,
    ReadyTimeout,
    SignalTimeout,
)
from .model import Case, IgnoreSpec, ServiceSpec, Step, Target, load_cases
from .results import (
    CallOutcome,
    CaseResult,
    CoverageMatrix,
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

    def __enter__(self) -> Session:
        self.start()
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

        if spec.mode == "attach":
            bus_env = attach_bus_env(BusType.SESSION)
            address = bus_env[BusType.SESSION.env_name]
        else:
            self.bus = PrivateBus(BusType.SESSION)
            address = self.bus.start()
            bus_env.update(self.bus.env)
            if self.keep:
                self.bus.keep()
            if spec.system_bus:
                self.system_bus = PrivateBus(BusType.SYSTEM)
                self.system_bus.start()
                bus_env.update(self.system_bus.env)
                if self.keep:
                    self.system_bus.keep()

        self.env = dict(bus_env)
        self.client = BusClient(address)

        if spec.mode == "attach":
            self.handle = self.launcher.attach(spec, address)
            return

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
                address,
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

        # 3) 拉起被测进程并等待就绪
        self.handle = self.launcher.launch(spec, address, overlay)
        try:
            self.launcher.wait_ready(self.handle, self.client)
        except (ProcessDied, ReadyTimeout):
            # 依赖 mock 不保真是最常见根因,优先给出精确缺失方法名
            if self.mocks.active:
                self.mocks.check_faithful(self.handle.output_tail(200))
            raise

    def stop(self) -> None:
        if self.handle is not None and not self.keep:
            self.handle.terminate()
        if self.client is not None and not self.keep:
            self.client.close()
        if not self.keep:
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
    client: BusClient
    guard: AttachGuard
    mocks: MockDeps
    baseline: str
    ignore: IgnoreSpec
    tests_dir: Path
    timeout_scale: float = 1.0
    handle: ServiceHandle | None = None
    live_xml: str = ""
    contract_deltas: list[Delta] = field(default_factory=list)


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
    if exp.value is not step.expect.__class__.__dataclass_fields__["value"].default:
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
        timeout = self._timeout(step, ctx, 5.0)
        got = wait_until(lambda: bool(received), timeout)
        if not got:
            raise SignalTimeout(
                f"等待信号 {step.target.member} 超时({timeout}s)",
                signal=step.target.member,
                received=list(received),
            )
        payload = received[0]
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
            result.cases.append(self.run_case(case, ctx))
        result.elapsed = time.monotonic() - started
        result.contract = list(ctx.contract_deltas)
        result.live_xml = ctx.live_xml
        return result


# --------------------------------------------------------------------------- 覆盖矩阵


def _member_kind_order(kind: str) -> int:
    return {"method": 0, "signal": 1, "property": 2}.get(kind, 3)


def build_coverage(
    baseline_xml: str,
    runtime_xml: str,
    src_xml: str | None,
    cases: list[Case],
) -> CoverageMatrix:
    """声明覆盖(源码/基线/运行时三方一致性)+ 执行覆盖(被哪些用例调用过)。"""
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

    # 执行覆盖:从用例的 Target 反查
    for case in cases:
        for step in case.steps:
            if step.target is None:
                continue
            cov = matrix.get(step.target.interface, step.target.member)
            if cov is None:
                kind = {
                    "call": "method",
                    "get-prop": "property",
                    "set-prop": "property",
                    "wait-signal": "signal",
                }.get(step.op)
                if kind is None:
                    continue
                cov = MemberCoverage(
                    interface=step.target.interface,
                    member=step.target.member,
                    kind=kind,  # type: ignore[arg-type]
                    declared="unknown",
                )
                matrix.add(cov)
            if case.name not in cov.executed_by:
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
) -> str:
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

    with Session(spec, keep=keep) as session:
        assert session.client is not None
        ctx = RunContext(
            spec=spec,
            client=session.client,
            guard=AttachGuard(spec),
            mocks=session.mocks,
            baseline=baseline,
            ignore=spec.ignore,
            tests_dir=tests_dir,
            timeout_scale=timeout_scale,
            handle=session.handle,
        )
        result = engine.run(cases, ctx)
        if not ctx.live_xml:
            # 补一次快照供覆盖矩阵使用;服务此时可能已被前面的用例弄成不可用
            # (例如刚测过一个会阻塞的方法),快照失败不应把整轮判为错误。
            try:
                tree = snapshot(session.client, spec.services, spec.ignore)
                ctx.live_xml = normalize_tree(tree, spec.ignore)
                result.live_xml = ctx.live_xml
            except DbusTestingError as exc:
                log.warning("收尾快照失败(不影响用例结果): %s", exc)
        result.env = session.env_fingerprint()

    if include_static:
        if src_xml is None:
            result.static_skipped = "仓内未找到源码声明 XML(src-xml-globs 无命中)"
        elif baseline:
            result.static_contract = static_diff(src_xml, baseline)

    result.coverage = build_coverage(baseline, result.live_xml, src_xml, cases)
    return SuiteOutcome(result=result, spec=spec, cases=cases)
