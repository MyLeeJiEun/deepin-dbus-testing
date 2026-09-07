"""报告产出、init 脚手架、CLI 退出码、pytest 插件(P1/P3 验收)。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from dbus_testing.engine import run_suite
from dbus_testing.errors import EXIT_CONFIG, EXIT_ENV, EXIT_FAILED, EXIT_OK
from dbus_testing.report import write_html, write_json, write_junit
from tests.conftest import make_suite, run_cli
from tests.fixtures import fixture_path

ECHO = "org.example.FxEcho"
GOOD_CASES = """apiVersion: v1
cases:
  - name: echo-ok
    call: {method: Echo, args: ["a"]}
    expect: {value: "a"}
  - name: boom-error
    call: {method: Boom}
    expect: {error: "*"}
  - name: counter-type
    get-prop: {property: Counter}
    expect: {type: i}
"""
BAD_CASES = """apiVersion: v1
cases:
  - name: will-fail
    call: {method: Echo, args: ["a"]}
    expect: {value: "b"}
"""


def _plugin_args() -> list[str]:
    """插件已通过 entry point 自动加载时不要再 -p,否则 pluggy 报重复注册。"""
    from importlib.metadata import entry_points

    for ep in entry_points(group="pytest11"):
        if ep.value == "dbus_testing.pytest_plugin":
            return []
    return ["-p", "dbus_testing.pytest_plugin"]


# --------------------------------------------------------------------------- 报告


def test_reports_are_written_and_wellformed(tmp_path: Path) -> None:
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,), cases=GOOD_CASES)
    result = run_suite(suite, include_static=False).result
    out = tmp_path / "report"
    out.mkdir()
    write_junit(result, out / "junit.xml")
    write_json(result, out / "results.json")
    write_html(result, out / "report.html")

    tree = ElementTree.parse(out / "junit.xml")
    suite_el = tree.getroot() if tree.getroot().tag == "testsuite" else tree.getroot()[0]
    assert suite_el.get("tests") == "3"
    assert suite_el.get("failures") == "0"
    names = {tc.get("name") for tc in suite_el.iter("testcase")}
    assert names == {"echo-ok", "boom-error", "counter-type"}

    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 3
    assert data["summary"]["passed"] == 3

    html = (out / "report.html").read_text(encoding="utf-8")
    for name in ("echo-ok", "boom-error", "counter-type"):
        assert name in html
    assert 'id="results"' in html


def test_junit_marks_failure_with_code(tmp_path: Path) -> None:
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,), cases=BAD_CASES)
    result = run_suite(suite, include_static=False).result
    out = tmp_path / "r"
    out.mkdir()
    write_junit(result, out / "junit.xml")
    text = (out / "junit.xml").read_text(encoding="utf-8")
    assert "<failure" in text
    assert "E_ASSERT" in text


# --------------------------------------------------------------------------- CLI


def test_cli_run_exit_codes(tmp_path: Path) -> None:
    ok_suite = make_suite(tmp_path / "ok", "fx_echo", services=(ECHO,), cases=GOOD_CASES)
    code, out, _err = run_cli(["run", str(ok_suite), "--no-static"])
    assert code == EXIT_OK, out
    assert "通过 3" in out

    bad_suite = make_suite(tmp_path / "bad", "fx_echo", services=(ECHO,), cases=BAD_CASES)
    code, out, _err = run_cli(["run", str(bad_suite), "--no-static"])
    assert code == EXIT_FAILED
    assert "FAIL" in out


def test_cli_config_error_exit_code(tmp_path: Path) -> None:
    suite = tmp_path / "dbus"
    suite.mkdir()
    (suite / "service.yaml").write_text(
        "apiVersion: v1\nservices: [a]\nbogus: 1\n", encoding="utf-8"
    )
    code, _out, err = run_cli(["run", str(suite), "--no-static"])
    assert code == EXIT_CONFIG
    assert "E_CONFIG_INVALID" in err


def test_cli_env_error_exit_code(tmp_path: Path) -> None:
    suite = tmp_path / "dbus"
    suite.mkdir()
    (suite / "service.yaml").write_text(
        "apiVersion: v1\nservices: [org.example.Nope]\nbinary:\n  search:\n    - /nope/x\n",
        encoding="utf-8",
    )
    code, _out, err = run_cli(["run", str(suite), "--no-static"])
    assert code == EXIT_ENV
    assert "E_BINARY_NOT_FOUND" in err


def test_cli_scan_and_check_roundtrip(tmp_path: Path) -> None:
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,))
    code, out, _err = run_cli(["scan", str(suite), "--emit"])
    assert code == EXIT_OK, out
    contract = (suite / "contract.xml").read_text(encoding="utf-8")
    assert "org.example.FxEcho" in contract

    code, out, _err = run_cli(["check", str(suite)])
    assert code == EXIT_OK
    assert "一致" in out

    # 人为改坏基线 → check 必须失败并给出 diff
    (suite / "contract.xml").write_text(
        contract.replace('<method name="Echo">', '<method name="EchoGone">'), encoding="utf-8"
    )
    code, out, _err = run_cli(["check", str(suite)])
    assert code == EXIT_FAILED
    assert "差异" in out


def test_cli_check_static_skips_without_src_xml(tmp_path: Path) -> None:
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,))
    run_cli(["scan", str(suite), "--emit"])
    code, out, _err = run_cli(["check", str(suite), "--static"])
    assert code == EXIT_OK
    assert "SKIP" in out


def test_cli_check_static_compares_src_xml(tmp_path: Path) -> None:
    """有源码声明 XML 时,--static 不起服务也能比出差异。"""
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,))
    run_cli(["scan", str(suite), "--emit"])
    # 在"仓库根"(tmp_path)下放一份与基线不一致的源码声明
    api = tmp_path / "api" / "dbus"
    api.mkdir(parents=True)
    (api / "echo.xml").write_text(
        """<node><interface name="org.example.FxEcho">
        <method name="Echo"><arg name="text" type="s"/><arg type="s" direction="out"/></method>
        </interface></node>""",
        encoding="utf-8",
    )
    (suite / "service.yaml").write_text(
        (suite / "service.yaml").read_text(encoding="utf-8")
        + f'src-xml-globs: ["{api}/*.xml"]\n',
        encoding="utf-8",
    )
    code, out, _err = run_cli(["check", str(suite), "--static"])
    # 源码只声明了 Echo,基线还有 Sum/Boom/Ping/属性/信号 → 必须报差异
    assert code == EXIT_FAILED
    assert "差异" in out


# --------------------------------------------------------------------------- init


def test_init_generates_runnable_suite(tmp_path: Path) -> None:
    """init 生成的配置**不加修改**即可 run 通过(P3 硬指标)。"""
    out_dir = tmp_path / "gen"
    args = ["init", "--binary", str(fixture_path("fx_echo")), "--out", str(out_dir)]
    code, out, err = run_cli(args)
    assert code == EXIT_OK, out + err
    assert (out_dir / "service.yaml").exists()
    assert (out_dir / "contract.xml").exists()
    assert (out_dir / "tests.yaml").exists()
    assert ECHO in (out_dir / "service.yaml").read_text(encoding="utf-8")

    code, out, _err = run_cli(["run", str(out_dir), "--no-static"])
    assert code == EXIT_OK, out
    assert "失败 0" in out


def test_init_draft_shape(tmp_path: Path) -> None:
    """草稿形态:属性全部生成可直接启用的用例;方法一律注释态(init 不主动调用方法)。

    设计决策:init 不会去调用被测方法 —— 无法预知副作用(可能挂起、可能改系统状态),
    所以方法只生成带签名提示的注释态草稿,由开发者补参数后启用。
    """
    out_dir = tmp_path / "gen"
    run_cli(["init", "--binary", str(fixture_path("fx_echo")), "--out", str(out_dir)])
    text = (out_dir / "tests.yaml").read_text(encoding="utf-8")
    active = [ln.strip() for ln in text.splitlines() if ln.startswith("  - name:")]

    # 两个属性:可读断言各一条;只读属性额外一条 PropertyReadOnly 错误路径
    assert "- name: counter-readable" in active
    assert "- name: name-readable" in active
    assert "- name: name-readonly" in active
    # 契约检查恒启用
    assert "- name: contract" in active
    assert "check-contract: true" in text

    # 方法为注释态,且带出入参签名提示,便于补参数
    assert "# - name: echo-smoke" in text
    assert "入参签名: s" in text
    assert "# - name: sum-smoke" in text
    assert "入参签名: ai" in text
    # 信号给出 wait-signal 模板
    assert "# - name: pinged-emitted" in text
    assert "wait-signal: {name: Pinged" in text


def test_init_does_not_overwrite_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "gen"
    run_cli(["init", "--binary", str(fixture_path("fx_echo")), "--out", str(out_dir)])
    marker = "# 手工修改过\n"
    (out_dir / "tests.yaml").write_text(marker, encoding="utf-8")
    args = ["init", "--binary", str(fixture_path("fx_echo")), "--out", str(out_dir)]
    code, out, _err = run_cli(args)
    assert code == EXIT_OK
    assert "跳过" in out
    assert (out_dir / "tests.yaml").read_text(encoding="utf-8") == marker


def test_init_recovers_from_missing_display(tmp_path: Path) -> None:
    """探测到疑似缺显示环境时自动重试 offscreen —— 用 fx_crash 验证重试路径被走过。"""
    out_dir = tmp_path / "gen"
    args = ["init", "--binary", str(fixture_path("fx_crash")), "--out", str(out_dir)]
    code, _out, err = run_cli(args)
    # fx_crash 永远起不来,但错误必须是带提示的 E_PROC_DIED,而不是裸异常
    assert code == EXIT_ENV
    assert "E_PROC_DIED" in err
    assert "QT_QPA_PLATFORM=offscreen" in err


# --------------------------------------------------------------------------- 构建产物解析


def test_binary_search_resolves_build_dir(tmp_path: Path) -> None:
    """${BUILD_DIR} 候选优先命中构建产物(P1 未验证环节的验证)。"""
    build = tmp_path / "build"
    (build / "bin").mkdir(parents=True)
    target = build / "bin" / "fx_echo"
    shutil.copy2(fixture_path("fx_echo"), target)
    target.chmod(0o755)

    suite = tmp_path / "dbus"
    suite.mkdir()
    (suite / "service.yaml").write_text(
        f"""apiVersion: v1
services: [{ECHO}]
binary:
  search:
    - ${{BUILD_DIR}}/bin/fx_echo
    - {fixture_path('fx_echo')}
sandbox:
  home: tmp
""",
        encoding="utf-8",
    )
    (suite / "tests.yaml").write_text(GOOD_CASES, encoding="utf-8")
    outcome = run_suite(suite, build_dir=build, include_static=False)
    assert outcome.result.ok
    assert outcome.result.env["binary"] == str(target), "应优先用构建产物而非系统兜底"


# --------------------------------------------------------------------------- pytest 插件


def test_pytest_plugin_collects_yaml_cases(tmp_path: Path) -> None:
    """插件把 tests.yaml 编译成原生 pytest 用例,且能定位到行号。"""
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,), cases=GOOD_CASES)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(suite), *_plugin_args(), "-v"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert "echo-ok" in combined, combined[-3000:]
    assert "boom-error" in combined
    assert "3 passed" in combined, combined[-3000:]


def test_pytest_plugin_reports_failure_detail(tmp_path: Path) -> None:
    suite = make_suite(tmp_path, "fx_echo", services=(ECHO,), cases=BAD_CASES)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(suite), *_plugin_args()],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert "1 failed" in combined, combined[-3000:]
    assert "返回值不符" in combined
