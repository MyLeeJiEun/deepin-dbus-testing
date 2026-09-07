"""system-bus: true 的端到端自测(P4/实测结论的固化,不依赖 DDE)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbus_testing.engine import run_suite
from tests.conftest import make_suite

CASES = """apiVersion: v1
cases:
  - name: echo-value
    call: {method: Echo, args: ["x"]}
    expect: {value: "sys:x"}
  - name: count-type
    get-prop: {property: Count}
    expect: {type: i}
  - name: contract
    check-contract: true
"""


def test_system_bus_isolate(tmp_path: Path) -> None:
    """system-bus: true:客户端与就绪探测都走私有 system bus。"""
    suite = make_suite(
        tmp_path,
        "fx_sysecho",
        services=("org.example.SysEcho",),
        cases=CASES,
        extra_service="system-bus: true\n",
    )
    outcome = run_suite(suite, include_static=False)
    result = outcome.result
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]
    # 环境指纹必须体现私有 system bus
    assert "system_bus_address" in result.env
    # 契约基线应包含 system bus 上的接口
    assert "org.example.SysEcho" in result.live_xml


def test_system_bus_session_bus_both_started(tmp_path: Path) -> None:
    """会话总线仍被创建(服务可能同时用到),但客户端走 system bus。"""
    suite = make_suite(
        tmp_path,
        "fx_sysecho",
        services=("org.example.SysEcho",),
        cases=CASES,
        extra_service="system-bus: true\n",
    )
    outcome = run_suite(suite, include_static=False)
    env = outcome.result.env
    assert env.get("bus_address", "").startswith("unix:path=")
    assert "system_bus_address" in env


def test_system_and_session_mocks_do_not_interfere(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """needs 混合编排:同名 mock 模板分别放到 session 与 system 总线,互不干扰。"""
    import dbus as _d
    _orig = type(_d.bus.BusConnection).__new__ if False else None
    suite = make_suite(
        tmp_path,
        "fx_sysecho",
        services=("org.example.SysEcho",),
        cases="""apiVersion: v1
cases:
  - name: echo-after-mock-state
    steps:
      - mock-state: {template: fx_dep, method: Subscribe}
      - call: {method: Echo, args: ["x"]}
        expect: {value: "sys:x"}
""",
        extra_service=(
            "system-bus: true\n"
            "needs:\n"
            "  - mock: fx_dep\n"
            "    bus: system\n"
            "  - mock: fx_dep\n"
            "    bus: session\n"
        ),
    )
    result = run_suite(suite, include_static=False).result
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]
