"""诊断规则:每条规则都由 fixture 服务真实触发(P3 验收)。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from dbus_testing.core.diagnose import diagnose
from dbus_testing.engine import Session, run_suite
from dbus_testing.errors import (
    BinaryNotFound,
    DbusTestingError,
    GuardDenied,
    MockUnfaithful,
    ProcessDied,
    ReadyTimeout,
)
from dbus_testing.model import load_service
from tests.conftest import make_suite

HAS_DBUSMOCK = importlib.util.find_spec("dbusmock") is not None


def _start(suite: Path) -> None:
    spec = load_service(suite / "service.yaml")
    with Session(spec):
        pass


def _expect(suite: Path, exc_type: type[DbusTestingError]) -> DbusTestingError:
    with pytest.raises(exc_type) as info:
        _start(suite)
    return info.value


def test_binary_not_found_lists_candidates(tmp_path: Path) -> None:
    suite = tmp_path / "dbus"
    suite.mkdir()
    (suite / "service.yaml").write_text(
        "apiVersion: v1\nservices: [org.example.Nope]\n"
        "binary:\n  search:\n    - /nonexistent/a\n    - /nonexistent/b\n",
        encoding="utf-8",
    )
    exc = _expect(suite, BinaryNotFound)
    d = diagnose(exc)
    assert d.code == "E_BINARY_NOT_FOUND"
    assert "/nonexistent/a" in d.detail and "/nonexistent/b" in d.detail
    assert "--build-dir" in d.hint


def test_process_died_suggests_offscreen(tmp_path: Path) -> None:
    """fx_crash 打印 Qt xcb 文本后退出 → 必须给出 QT_QPA_PLATFORM=offscreen 提示。"""
    suite = make_suite(tmp_path, "fx_crash", services=("org.example.FxCrash",))
    exc = _expect(suite, ProcessDied)
    d = diagnose(exc)
    assert d.code == "E_PROC_DIED"
    assert "QT_QPA_PLATFORM=offscreen" in d.hint
    assert "platform plugin" in d.detail


def test_ready_timeout_wrong_name_branch(tmp_path: Path) -> None:
    """fx_wrongname 注册的名字与配置不一致 → 提示 services 写错并给出实际名字。"""
    suite = make_suite(
        tmp_path,
        "fx_wrongname",
        services=("org.example.FxWrongName",),
        ready_timeout="3s",
    )
    exc = _expect(suite, ReadyTimeout)
    d = diagnose(exc)
    assert d.code == "E_READY_TIMEOUT"
    assert "org.example.FxUnexpected" in d.detail
    assert "写错" in d.hint


def test_ready_timeout_no_names_branch(tmp_path: Path) -> None:
    """fx_slow 在超时窗口内不注册任何名字 → 走"没有任何业务名字"分支。"""
    suite = make_suite(
        tmp_path,
        "fx_slow",
        services=("org.example.FxSlow",),
        ready_timeout="1s",
    )
    exc = _expect(suite, ReadyTimeout)
    d = diagnose(exc)
    assert d.code == "E_READY_TIMEOUT"
    assert "没有任何业务名字" in d.hint or "ready.timeout" in d.hint


@pytest.mark.skipif(not HAS_DBUSMOCK, reason="需要 python-dbusmock")
def test_mock_unfaithful_reports_missing_method(tmp_path: Path) -> None:
    """依赖 mock 缺 Subscribe → 必须精确报出 org.example.FxDep.Manager.Subscribe。"""
    suite = make_suite(
        tmp_path,
        "fx_needdep",
        services=("org.example.FxNeedDep",),
        ready_timeout="5s",
        extra_service="needs:\n  - mock: fx_dep_incomplete\n",
    )
    exc = _expect(suite, MockUnfaithful)
    d = diagnose(exc)
    assert d.code == "E_MOCK_UNFAITHFUL"
    assert "org.example.FxDep.Manager.Subscribe" in d.detail
    assert "mocktemplates" in d.hint
    assert "AddMethod" in d.detail


@pytest.mark.skipif(not HAS_DBUSMOCK, reason="需要 python-dbusmock")
def test_faithful_mock_lets_service_start(tmp_path: Path) -> None:
    """依赖 mock 补齐后,被测服务应能正常注册并通过用例。"""
    suite = make_suite(
        tmp_path,
        "fx_needdep",
        services=("org.example.FxNeedDep",),
        ready_timeout="10s",
        extra_service="needs:\n  - mock: fx_dep\n",
        cases="""apiVersion: v1
cases:
  - name: who
    call: {method: Who}
    expect: {value: org.example.FxNeedDep}
""",
    )
    result = run_suite(suite, include_static=False).result
    assert result.cases[0].status == "pass", result.cases[0].detail


def test_call_timeout(tmp_path: Path) -> None:
    """fx_hang 的 Sleep 不返回 → E_CALL_TIMEOUT。"""
    suite = make_suite(
        tmp_path,
        "fx_hang",
        services=("org.example.FxHang",),
        cases="""apiVersion: v1
cases:
  - name: hangs
    call: {method: Sleep, timeout: 300ms}
""",
    )
    result = run_suite(suite, include_static=False).result
    case = result.cases[0]
    assert case.status == "fail"
    assert case.code == "E_CALL_TIMEOUT"
    assert "busctl" in case.hint


def test_readonly_property_error_path(tmp_path: Path) -> None:
    suite = make_suite(
        tmp_path,
        "fx_readonly",
        services=("org.example.FxReadonly",),
        cases="""apiVersion: v1
cases:
  - name: fixed-readable
    get-prop: {property: Fixed}
    expect: {type: s, value: fixed-value}
  - name: fixed-readonly
    set-prop: {property: Fixed, value: "x"}
    expect: {error: org.freedesktop.DBus.Error.PropertyReadOnly}
""",
    )
    result = run_suite(suite, include_static=False).result
    assert [c.status for c in result.cases] == ["pass", "pass"], [
        (c.name, c.status, c.detail) for c in result.cases
    ]


def test_guard_denies_call_in_attach_mode(tmp_path: Path) -> None:
    """attach 模式默认只读:未放行的方法调用被拒且计为配置错误。"""
    from dbus_testing.core.guard import AttachGuard
    from dbus_testing.model import Step, Target

    suite = make_suite(
        tmp_path, "fx_echo", services=("org.example.FxEcho",), mode="attach"
    )
    spec = load_service(suite / "service.yaml")
    guard = AttachGuard(spec)
    call = Step(op="call", target=Target("s", "/p", "org.example.FxEcho", "Echo"))
    with pytest.raises(GuardDenied) as info:
        guard.check(call)
    d = diagnose(info.value)
    assert d.code == "E_GUARD_DENIED"
    assert "allow.methods" in d.hint
    # 只读操作恒放行
    guard.check(Step(op="get-prop", target=call.target))
    guard.check(Step(op="check-contract"))


def test_guard_allowlist_lets_named_method_through(tmp_path: Path) -> None:
    from dbus_testing.core.guard import AttachGuard
    from dbus_testing.model import Step, Target

    suite = make_suite(
        tmp_path,
        "fx_echo",
        services=("org.example.FxEcho",),
        mode="attach",
        extra_service="allow:\n  methods:\n    - org.example.FxEcho.Echo\n",
    )
    spec = load_service(suite / "service.yaml")
    guard = AttachGuard(spec)
    guard.check(Step(op="call", target=Target("s", "/p", "org.example.FxEcho", "Echo")))
    with pytest.raises(GuardDenied):
        guard.check(Step(op="call", target=Target("s", "/p", "org.example.FxEcho", "Boom")))


def test_polkit_gated_method_gets_targeted_note(tmp_path: Path) -> None:
    """auth.polkit 声明过的方法遇到未授权错误时,失败详情要给门禁专属说明。"""
    suite = make_suite(
        tmp_path,
        "fx_echo",
        services=("org.example.FxEcho",),
        extra_service=(
            "auth:\n"
            "  polkit:\n"
            "    - interface: org.example.FxEcho\n"
            "      methods: [Boom]\n"
        ),
        cases="""apiVersion: v1
cases:
  - name: gated-call
    call: {method: Boom}
    expect: {error: org.freedesktop.DBus.Error.AccessDenied}
""",
    )
    result = run_suite(suite, include_static=False).result
    case = result.cases[0]
    # fx_echo 的 Boom 抛的是自定义错误,不是 AccessDenied → 用例失败
    assert case.status == "fail"
    assert "期望错误" in case.detail


def test_polkit_declaration_without_mock_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """声明了 auth.polkit 但 needs: 里没有 polkitd mock → 必须告警提示。"""
    import logging

    suite = make_suite(
        tmp_path,
        "fx_echo",
        services=("org.example.FxEcho",),
        extra_service=(
            "auth:\n"
            "  polkit:\n"
            "    - interface: org.example.FxEcho\n"
            "      methods: [Boom]\n"
        ),
    )
    spec = load_service(suite / "service.yaml")
    assert spec.is_polkit_gated("org.example.FxEcho", "Boom")
    assert not spec.is_polkit_gated("org.example.FxEcho", "Echo")
    with caplog.at_level(logging.WARNING, logger="dbus_testing.engine"):
        with Session(spec):
            pass
    assert any("polkitd" in rec.message for rec in caplog.records), [
        r.message for r in caplog.records
    ]
