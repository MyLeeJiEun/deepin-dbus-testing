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


def test_attach_honors_system_bus(tmp_path: Path) -> None:
    """attach + system-bus: true:主交互总线必须切到 system 总线(R3 回归)。

    修复前 attach 分支硬编码只读 session 地址,system 总线上的服务完全
    不可见,scan 报 0 个对象且退出码 0。
    """
    import os
    import subprocess
    import sys
    import time

    from dbus_testing.core.bus import BusType, PrivateBus
    from dbus_testing.engine import Session
    from dbus_testing.model import load_service
    from dbus_testing.scanner import normalize_tree, snapshot
    from tests.fixtures import fixture_path

    suite = make_suite(
        tmp_path,
        "fx_sysecho",
        services=("org.example.SysEcho",),
        mode="attach",
        extra_service="system-bus: true\n",
    )
    spec = load_service(suite / "service.yaml")
    sbus = PrivateBus(BusType.SESSION)
    ybus = PrivateBus(BusType.SYSTEM)
    saddr, yaddr = sbus.start(), ybus.start()
    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = saddr
    env["DBUS_SYSTEM_BUS_ADDRESS"] = yaddr
    proc = subprocess.Popen(
        [sys.executable, str(fixture_path("fx_sysecho"))],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    saved = (
        os.environ.get(BusType.SESSION.env_name),
        os.environ.get(BusType.SYSTEM.env_name),
    )
    try:
        time.sleep(1.0)
        os.environ[BusType.SESSION.env_name] = saddr
        os.environ[BusType.SYSTEM.env_name] = yaddr
        with Session(spec) as session:
            assert session.client is not None
            tree = snapshot(session.client, spec.services, spec.ignore)
            contract = normalize_tree(tree, spec.ignore)
        assert 'interface name="org.example.SysEcho"' in contract, contract
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        for key, value in zip(
            (BusType.SESSION.env_name, BusType.SYSTEM.env_name), saved, strict=True
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sbus.stop()
        ybus.stop()


def test_attach_system_bus_requires_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attach + system-bus: true 但环境没有 DBUS_SYSTEM_BUS_ADDRESS → fail-fast。

    SESSION 地址必须显式设置:attach 分支先取 SESSION 再查 SYSTEM,
    依赖外部环境恰有 SESSION 变量会让这条测试换个环境就失败。
    SESSION 只在此处被读取(raise 发生在 BusClient 连接之前),无需真实总线。
    """
    from dbus_testing.core.bus import BusType
    from dbus_testing.engine import Session
    from dbus_testing.errors import DbusTestingError
    from dbus_testing.model import load_service

    suite = make_suite(
        tmp_path,
        "fx_sysecho",
        services=("org.example.SysEcho",),
        mode="attach",
        extra_service="system-bus: true\n",
    )
    spec = load_service(suite / "service.yaml")
    monkeypatch.delenv(BusType.SYSTEM.env_name, raising=False)
    monkeypatch.setenv(BusType.SESSION.env_name, "unix:path=/tmp/dbus-testing-unused")
    with pytest.raises(DbusTestingError, match="DBUS_SYSTEM_BUS_ADDRESS"):
        session = Session(spec)
        try:
            session.start()
        finally:
            session.stop()
