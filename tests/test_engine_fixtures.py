"""端到端:六原语 × 五断言,针对 fx_echo 等 fixture 服务(P1/P3 验收)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dbus_testing.engine import run_suite
from tests.conftest import make_suite

ECHO = "org.example.FxEcho"
IFACE_KV = ""  # fx_echo 的接口名与服务名相同,Target 可缺省


def _suite(tmp_path: Path, cases: str, **kw: Any) -> Path:
    return make_suite(tmp_path, "fx_echo", services=(ECHO,), cases=cases, **kw)


def _run(suite: Path) -> Any:
    return run_suite(suite, include_static=False).result


def test_call_with_value_and_signature(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: echo-value
    call: {method: Echo, args: ["深度"]}
    expect: {signature: s, value: "深度"}
  - name: sum-value
    call: {method: Sum, args: [[1, 2, 39]]}
    expect: {signature: i, value: 42}
  - name: echo-nonempty
    call: {method: Echo, args: ["x"]}
    expect: {nonempty: true}
""",
    )
    result = _run(suite)
    assert [c.status for c in result.cases] == ["pass", "pass", "pass"], [
        (c.name, c.status, c.detail) for c in result.cases
    ]


def test_value_mismatch_fails_with_detail(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: wrong-value
    call: {method: Echo, args: ["a"]}
    expect: {value: "b"}
""",
    )
    result = _run(suite)
    assert result.cases[0].status == "fail"
    assert "返回值不符" in result.cases[0].detail


def test_signature_mismatch_fails(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: wrong-signature
    call: {method: Echo, args: ["a"]}
    expect: {signature: i}
""",
    )
    result = _run(suite)
    assert result.cases[0].status == "fail"
    assert "签名不符" in result.cases[0].detail


def test_error_paths(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: boom-any-error
    call: {method: Boom}
    expect: {error: "*"}
  - name: boom-named-error
    call: {method: Boom}
    expect: {error: org.example.FxEcho.Error.Boom}
  - name: unknown-method
    call: {method: NoSuchMethod}
    expect: {error: org.freedesktop.DBus.Error.UnknownMethod}
  - name: expected-ok-but-errors
    call: {method: Boom}
    expect: {error: null}
""",
    )
    result = _run(suite)
    statuses = {c.name: c.status for c in result.cases}
    assert statuses["boom-any-error"] == "pass"
    assert statuses["boom-named-error"] == "pass"
    assert statuses["unknown-method"] == "pass"
    assert statuses["expected-ok-but-errors"] == "fail"


def test_wrong_error_name_fails(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: wrong-error
    call: {method: Boom}
    expect: {error: org.freedesktop.DBus.Error.AccessDenied}
""",
    )
    result = _run(suite)
    assert result.cases[0].status == "fail"
    assert "期望错误" in result.cases[0].detail


def test_property_read_write_and_readonly(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: counter-type
    get-prop: {property: Counter}
    expect: {type: i}
  - name: counter-roundtrip
    steps:
      - set-prop: {property: Counter, value: 7}
      - get-prop: {property: Counter}
        expect: {value: 7}
  - name: name-readonly
    set-prop: {property: Name, value: "nope"}
    expect: {error: org.freedesktop.DBus.Error.PropertyReadOnly}
  - name: unknown-property
    get-prop: {property: Nope}
    expect: {error: org.freedesktop.DBus.Error.UnknownProperty}
""",
    )
    result = _run(suite)
    statuses = {c.name: (c.status, c.detail) for c in result.cases}
    assert statuses["counter-type"][0] == "pass", statuses["counter-type"]
    assert statuses["counter-roundtrip"][0] == "pass", statuses["counter-roundtrip"]
    assert statuses["name-readonly"][0] == "pass", statuses["name-readonly"]
    assert statuses["unknown-property"][0] == "pass", statuses["unknown-property"]


def test_wait_signal_after_call(tmp_path: Path) -> None:
    """call + wait-signal 组合:布扣必须先于触发。"""
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: ping-emits
    call: {method: Ping}
    wait-signal: {name: Pinged, timeout: 5s}
""",
    )
    result = _run(suite)
    assert result.cases[0].status == "pass", result.cases[0].detail


def test_wait_signal_timeout_reports_code(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: never-emitted
    wait-signal: {name: NeverHappens, timeout: 300ms}
""",
    )
    result = _run(suite)
    case = result.cases[0]
    assert case.status == "fail"
    assert case.code == "E_SIGNAL_TIMEOUT"
    assert "等待信号" in case.summary


def test_check_contract_pass_and_drift(tmp_path: Path) -> None:
    from dbus_testing.core.bus import BusType, PrivateBus
    from dbus_testing.core.client import BusClient
    from dbus_testing.core.launcher import Launcher
    from dbus_testing.core.sandbox import Sandbox
    from dbus_testing.model import IgnoreSpec, SandboxSpec, load_service
    from dbus_testing.scanner import normalize_tree
    from dbus_testing.scanner.introspect import snapshot

    cases = "apiVersion: v1\ncases:\n  - name: contract\n    check-contract: true\n"
    suite = _suite(tmp_path, cases)

    # 先用 scan 的等价流程生成基线
    spec = load_service(suite / "service.yaml")
    with PrivateBus(BusType.SESSION) as bus:
        client = BusClient(bus.address)
        sandbox = Sandbox(SandboxSpec())
        env = sandbox.__enter__()
        overlay = dict(bus.env)
        overlay.update(env)
        handle = Launcher().launch(spec, bus.address, overlay)
        Launcher().wait_ready(handle, client)
        baseline = normalize_tree(snapshot(client, spec.services, IgnoreSpec()), IgnoreSpec())
        handle.terminate()
        sandbox.__exit__(None, None, None)
        client.close()
    (suite / "contract.xml").write_text(baseline, encoding="utf-8")

    result = _run(suite)
    assert result.cases[0].status == "pass", result.cases[0].detail
    assert result.coverage.total > 0
    assert result.coverage.declared_ok == result.coverage.total

    # 人为删掉一个方法 → 必须报漂移
    broken = baseline.replace('<method name="Echo">', '<method name="EchoRenamed">')
    (suite / "contract.xml").write_text(broken, encoding="utf-8")
    result2 = _run(suite)
    case = result2.cases[0]
    assert case.status == "fail"
    assert case.code == "E_CONTRACT_DRIFT"
    assert result2.contract, "漂移列表应非空"


def test_multi_service_ready(tmp_path: Path) -> None:
    """单进程注册多个服务名:全部就绪才算 READY。"""
    suite = make_suite(
        tmp_path,
        "fx_multi",
        services=("org.example.FxMultiA", "org.example.FxMultiB", "org.example.FxMultiC"),
        cases="""apiVersion: v1
cases:
  - name: who-a
    call: {interface: org.example.FxMulti, object: /org/example/FxMultiA, method: Who}
    expect: {value: org.example.FxMultiA}
  - name: who-c
    call: {service: org.example.FxMultiC, interface: org.example.FxMulti,
           object: /org/example/FxMultiC, method: Who}
    expect: {value: org.example.FxMultiC}
""",
    )
    result = _run(suite)
    assert [c.status for c in result.cases] == ["pass", "pass"], [
        (c.name, c.status, c.detail) for c in result.cases
    ]


def test_flaky_retry_marks_case(tmp_path: Path) -> None:
    """flaky: N 显式声明才重试,且重试成功要在结果里可见。"""
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: always-fails
    flaky: 2
    call: {method: Echo, args: ["a"]}
    expect: {value: "b"}
""",
    )
    result = _run(suite)
    assert result.cases[0].status == "fail"
    assert result.cases[0].attempts == 3


def test_skip_case(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: postponed
    skip: 等待上游修复
    call: {method: Echo, args: ["a"]}
""",
    )
    result = _run(suite)
    assert result.cases[0].status == "skip"
    assert result.cases[0].summary == "等待上游修复"


def test_coverage_matrix_executed_by(tmp_path: Path) -> None:
    suite = _suite(
        tmp_path,
        """apiVersion: v1
cases:
  - name: echo-case
    call: {method: Echo, args: ["a"]}
  - name: counter-case
    get-prop: {property: Counter}
""",
    )
    result = _run(suite)
    cov = result.coverage.get(ECHO, "Echo")
    assert cov is not None and cov.executed_by == ["echo-case"]
    counter = result.coverage.get(ECHO, "Counter")
    assert counter is not None and counter.executed_by == ["counter-case"]
    boom = result.coverage.get(ECHO, "Boom")
    assert boom is not None and not boom.executed, "未被调用的方法不应计入执行覆盖"
