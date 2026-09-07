"""scanner:规范化、契约 diff、源码 XML 静态比对(P2 验收)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbus_testing.model import IgnoreSpec
from dbus_testing.scanner import (
    diff,
    normalize_interface_xml,
    normalize_tree,
    parse_members,
    src_baseline_xml,
    static_diff,
)

MESSY = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <!-- 注释应被剥离 -->
  <interface name="org.example.Zeta">
    <method name="Later"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.example.Demo">
    <property name="Counter" type="i" access="readwrite"/>
    <signal name="Pinged"><arg type="i"/></signal>
    <method name="Query">
      <annotation name="org.freedesktop.DBus.Description" value="忽略我"/>
      <arg name="hans" type="s" direction="in"/>
      <arg name="opts" type="s" direction="in"/>
      <arg name="out" type="as" direction="out"/>
    </method>
    <method name="Ping"/>
    <method name="DeprecatedQuery"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
  </interface>
  <node name="child"/>
</node>
"""

IGNORE = IgnoreSpec(
    interfaces=("org.freedesktop.DBus.*",), methods=("org.example.Demo.DeprecatedQuery",)
)


def test_normalize_sorts_and_strips() -> None:
    out = normalize_interface_xml(MESSY, IGNORE)
    assert out.index("org.example.Demo") < out.index("org.example.Zeta"), "接口按名排序"
    assert "org.freedesktop.DBus.Peer" not in out, "被忽略的接口应消失"
    assert "DeprecatedQuery" not in out, "被忽略的方法应消失"
    assert "annotation" not in out and "注释应被剥离" not in out
    # 同一接口内:method → signal → property
    assert out.index('<method name="Ping"') < out.index('<signal name="Pinged"')
    assert out.index('<signal name="Pinged"') < out.index('<property name="Counter"')
    # arg 保持声明顺序(顺序是语义,不能排序)
    assert out.index('name="hans"') < out.index('name="opts"') < out.index('name="out"')


def test_normalize_is_idempotent() -> None:
    once = normalize_interface_xml(MESSY, IGNORE)
    twice = normalize_interface_xml(once, IGNORE)
    assert once == twice
    assert normalize_interface_xml(twice, IGNORE) == once


def test_parse_members_signature_format() -> None:
    members = parse_members(normalize_interface_xml(MESSY, IGNORE))
    demo = members["org.example.Demo"]
    assert demo["Query"] == ("method", "in=ss,out=as")
    assert demo["Ping"] == ("method", "in=,out=")
    assert demo["Counter"] == ("property", "type=i,access=readwrite")
    assert demo["Pinged"] == ("signal", "i")


def test_diff_detects_removed_changed_and_added() -> None:
    baseline = normalize_interface_xml(MESSY, IGNORE)
    assert diff(baseline, baseline) == []

    removed = baseline.replace('<method name="Ping"/>\n', "")
    deltas = diff(baseline, removed)
    assert [d.kind for d in deltas] == ["removed"]
    assert deltas[0].member == "Ping"

    changed_sig = baseline.replace('type="as" direction="out"', 'type="s" direction="out"')
    deltas = diff(baseline, changed_sig)
    assert [d.kind for d in deltas] == ["changed"]
    assert deltas[0].member == "Query"
    assert "out=as" in (deltas[0].before or "") and "out=s" in (deltas[0].after or "")

    changed_access = baseline.replace('access="readwrite"', 'access="read"')
    deltas = diff(baseline, changed_access)
    assert [d.kind for d in deltas] == ["changed"]
    assert deltas[0].member == "Counter"


def test_normalize_tree_collapses_dynamic_children() -> None:
    tree = {
        "/org/example/Demo": MESSY,
        "/org/example/Demo/session/1": """<node><interface name="org.example.Session">
            <method name="Commit"/></interface></node>""",
    }
    ignore = IgnoreSpec(
        paths=("/org/example/Demo/session/*",), interfaces=("org.freedesktop.DBus.*",)
    )
    out = normalize_tree(tree, ignore)
    assert 'path="/org/example/Demo/session/*"' in out
    assert 'collapsed="true"' in out
    assert normalize_tree(tree, ignore) == out


def test_src_baseline_and_static_diff(tmp_path: Path) -> None:
    api = tmp_path / "api" / "dbus"
    api.mkdir(parents=True)
    (api / "org.example.Demo.xml").write_text(
        """<node><interface name="org.example.Demo">
        <method name="Query"><arg name="hans" type="s"/><arg name="opts" type="s"/>
        <arg type="as" direction="out"/></method>
        <property name="Counter" type="i" access="read"/>
        </interface></node>""",
        encoding="utf-8",
    )
    src = src_baseline_xml(tmp_path, ("api/dbus/*.xml",), IgnoreSpec())
    assert src is not None
    assert 'path="*"' in src

    baseline = normalize_tree({"/org/example/Demo": MESSY}, IGNORE)
    deltas = static_diff(src, baseline)
    kinds = {(d.kind, d.member) for d in deltas}
    # 源码里 Counter 是 read,基线里是 readwrite → changed
    assert ("changed", "Counter") in kinds
    # 基线里多出的接口 → added
    assert any(d.kind == "added" and d.interface == "org.example.Zeta" for d in deltas)
    # 源码 XML 省略 direction="in" 不应造成 Query 误报
    assert not any(d.member == "Query" for d in deltas), [d.render() for d in deltas]


def test_src_baseline_returns_none_when_no_xml(tmp_path: Path) -> None:
    assert src_baseline_xml(tmp_path, ("api/dbus/*.xml",), IgnoreSpec()) is None


@pytest.mark.needs_dde
def test_static_check_against_real_repo() -> None:
    """对真实仓(dde-application-manager)跑源码声明收集。"""
    repo = Path.home() / "src" / "dde-application-manager"
    if not (repo / "api" / "dbus").is_dir():
        pytest.skip("本机没有 dde-application-manager 源码")
    src = src_baseline_xml(repo, ("api/dbus/*.xml",), IgnoreSpec())
    assert src is not None
    members = parse_members(src)
    assert "org.desktopspec.ApplicationManager1" in members
    am = members["org.desktopspec.ApplicationManager1"]
    assert "Identify" in am
    assert "ReloadApplications" in am


def test_scan_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """同一服务连续两次 scan 的产物必须逐字节相同(规范化稳定性,P2 硬指标)。"""
    from tests.conftest import make_suite, run_cli

    suite = make_suite(tmp_path, "fx_echo", services=("org.example.FxEcho",))
    code, _out, err = run_cli(["scan", str(suite), "--emit"])
    assert code == 0, err
    first = (suite / "contract.xml").read_bytes()
    code, _out, err = run_cli(["scan", str(suite), "--emit"])
    assert code == 0, err
    second = (suite / "contract.xml").read_bytes()
    assert first == second, "两次 scan 的基线不一致 → 规范化不稳定"
