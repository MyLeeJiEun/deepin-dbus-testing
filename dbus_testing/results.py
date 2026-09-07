"""执行结果数据结构(engine 产出,report 消费)。

单独成模块,避免 report 与 engine 循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .model import Case, Step

CaseStatus = Literal["pass", "fail", "error", "config-error", "skip"]
DeclaredStatus = Literal[
    "ok", "only-src", "only-baseline", "only-runtime", "mismatch", "no-src", "unknown"
]


@dataclass(frozen=True)
class CallOutcome:
    """一次 DBus 交互的结果(client 层产出)。"""

    ok: bool
    value: Any = None
    signature: str = ""
    error_name: str | None = None
    error_message: str | None = None
    elapsed: float = 0.0


@dataclass
class StepResult:
    step: Step
    ok: bool
    detail: str = ""
    outcome: CallOutcome | None = None
    elapsed: float = 0.0

    @property
    def op(self) -> str:
        return self.step.op


@dataclass
class CaseResult:
    case: Case
    status: CaseStatus
    steps: list[StepResult] = field(default_factory=list)
    elapsed: float = 0.0
    attempts: int = 1
    code: str | None = None
    summary: str = ""
    detail: str = ""
    hint: str = ""

    @property
    def name(self) -> str:
        return self.case.name

    @property
    def flaky(self) -> bool:
        return self.attempts > 1 and self.status == "pass"


@dataclass(frozen=True)
class Delta:
    kind: Literal["added", "removed", "changed"]
    scope: Literal["interface", "method", "signal", "property", "arg", "path"]
    path: str
    interface: str
    member: str | None = None
    before: str | None = None
    after: str | None = None

    def render(self) -> str:
        loc = f"{self.path} {self.interface}"
        if self.member:
            loc += f".{self.member}"
        if self.kind == "changed":
            return f"[changed] {loc}: {self.before} -> {self.after}"
        detail = self.after if self.kind == "added" else self.before
        return f"[{self.kind}] {loc}" + (f": {detail}" if detail else "")


@dataclass
class MemberCoverage:
    interface: str
    member: str
    kind: Literal["method", "property", "signal"]
    declared: DeclaredStatus = "unknown"
    executed_by: list[str] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return bool(self.executed_by)


@dataclass
class CoverageMatrix:
    members: dict[str, dict[str, MemberCoverage]] = field(default_factory=dict)

    def add(self, cov: MemberCoverage) -> None:
        self.members.setdefault(cov.interface, {})[cov.member] = cov

    def get(self, interface: str, member: str) -> MemberCoverage | None:
        return self.members.get(interface, {}).get(member)

    def all_members(self) -> list[MemberCoverage]:
        return [m for iface in self.members.values() for m in iface.values()]

    @property
    def declared_ok(self) -> int:
        return sum(1 for m in self.all_members() if m.declared == "ok")

    @property
    def executed_count(self) -> int:
        return sum(1 for m in self.all_members() if m.executed)

    @property
    def total(self) -> int:
        return len(self.all_members())


@dataclass
class RunResult:
    cases: list[CaseResult] = field(default_factory=list)
    contract: list[Delta] = field(default_factory=list)
    static_contract: list[Delta] = field(default_factory=list)
    static_skipped: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    coverage: CoverageMatrix = field(default_factory=CoverageMatrix)
    elapsed: float = 0.0
    live_xml: str = ""

    def count(self, status: CaseStatus) -> int:
        return sum(1 for c in self.cases if c.status == status)

    @property
    def passed(self) -> int:
        return self.count("pass")

    @property
    def failed(self) -> int:
        return self.count("fail")

    @property
    def errored(self) -> int:
        return self.count("error")

    @property
    def config_errors(self) -> int:
        return self.count("config-error")

    @property
    def skipped(self) -> int:
        return self.count("skip")

    @property
    def flaky(self) -> int:
        return sum(1 for c in self.cases if c.flaky)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errored == 0 and self.config_errors == 0
