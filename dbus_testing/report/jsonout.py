"""results.json:RunResult 全量序列化,供趋势对比与二次分析.

手写转字典而不用 dataclasses.asdict:配置里带 Path、Expect 里带 UNSET 哨兵,
asdict 会直接抛 TypeError;这里统一走 _safe(),保结构、退化为字符串。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..model import Expect, Step, Target
from ..results import (
    CallOutcome,
    CaseResult,
    CoverageMatrix,
    Delta,
    MemberCoverage,
    RunResult,
    StepResult,
)

_PRIMITIVES = (str, bool, int, float)


def _safe(value: Any) -> Any:
    """尽量保留结构;Path 转字符串,其余不可 JSON 化对象(如 UNSET)退化为 repr。"""
    if value is None or isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return repr(value)


def _target(target: Target | None) -> dict[str, Any] | None:
    if target is None:
        return None
    return {
        "service": target.service,
        "path": target.path,
        "interface": target.interface,
        "member": target.member,
    }


def _expect(expect: Expect) -> dict[str, Any]:
    return {
        "signature": expect.signature,
        "value": _safe(expect.value),
        "type": expect.type,
        "error": _safe(expect.error),
        "nonempty": expect.nonempty,
        "expects_error": expect.expects_error,
    }


def _step(step: Step) -> dict[str, Any]:
    return {
        "op": step.op,
        "line": step.line,
        "target": _target(step.target),
        "args": [_safe(a) for a in step.args],
        "value": _safe(step.value),
        "timeout": step.timeout,
        "expect": _expect(step.expect),
        "signal": step.signal,
        "mock_template": step.mock_template,
        "mock_method": step.mock_method,
    }


def _outcome(outcome: CallOutcome | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    return {
        "ok": outcome.ok,
        "value": _safe(outcome.value),
        "signature": outcome.signature,
        "error_name": outcome.error_name,
        "error_message": outcome.error_message,
        "elapsed": outcome.elapsed,
    }


def _step_result(result: StepResult) -> dict[str, Any]:
    return {
        "op": result.op,
        "ok": result.ok,
        "detail": result.detail,
        "elapsed": result.elapsed,
        "step": _step(result.step),
        "outcome": _outcome(result.outcome),
    }


def _case_result(case: CaseResult) -> dict[str, Any]:
    return {
        "name": case.name,
        "status": case.status,
        "line": case.case.line,
        "elapsed": case.elapsed,
        "attempts": case.attempts,
        "retries": case.case.flaky,
        "flaky": case.flaky,
        "skip": case.case.skip,
        "code": case.code,
        "summary": case.summary,
        "detail": case.detail,
        "hint": case.hint,
        "steps": [_step_result(s) for s in case.steps],
    }


def _delta(delta: Delta) -> dict[str, Any]:
    return {
        "kind": delta.kind,
        "scope": delta.scope,
        "path": delta.path,
        "interface": delta.interface,
        "member": delta.member,
        "before": delta.before,
        "after": delta.after,
        "render": delta.render(),
    }


def _member(cov: MemberCoverage) -> dict[str, Any]:
    return {
        "interface": cov.interface,
        "member": cov.member,
        "kind": cov.kind,
        "declared": cov.declared,
        "executed": cov.executed,
        "executed_by": list(cov.executed_by),
    }


def _coverage(matrix: CoverageMatrix) -> dict[str, Any]:
    total = matrix.total
    return {
        "total": total,
        "declared_ok": matrix.declared_ok,
        "executed": matrix.executed_count,
        "declared_rate": matrix.declared_ok / total if total else 0.0,
        "executed_rate": matrix.executed_count / total if total else 0.0,
        "interfaces": {
            interface: [_member(m) for m in members.values()]
            for interface, members in matrix.members.items()
        },
    }


def result_to_dict(result: RunResult) -> dict[str, Any]:
    return {
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "errored": result.errored,
            "config_errors": result.config_errors,
            "skipped": result.skipped,
            "flaky": result.flaky,
            "ok": result.ok,
            "elapsed": result.elapsed,
        },
        "env": {str(k): str(v) for k, v in result.env.items()},
        "cases": [_case_result(c) for c in result.cases],
        "contract": [_delta(d) for d in result.contract],
        "static_contract": [_delta(d) for d in result.static_contract],
        "static_skipped": result.static_skipped,
        "coverage": _coverage(result.coverage),
        "live_xml": result.live_xml,
    }


def write_json(result: RunResult, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result_to_dict(result), indent=2, ensure_ascii=False)
    out_path.write_text(payload + "\n", encoding="utf-8")
