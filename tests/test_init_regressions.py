"""init 脚手架与草稿生成的回归测试。

这些用例覆盖的都是**在真实仓库实践时暴露、fixture 测不出来**的缺陷:
1. init 丢弃 --arg / `--` 之后的参数,导致生成的配置跑起来是另一回事;
2. 草稿只写 interface 不写 object,多服务名/多对象路径的进程会全线 InterfaceNotFound;
3. CamelCase→kebab 对连续大写缩写处理错误(CanNTP → can-n-t-p);
4. 只读属性草稿写死 PropertyReadOnly,而各实现返回的错误名不同;
5. go-loader 的 -i 与 launcher 自动补的 -i 重复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbus_testing.core.launcher import Launcher
from dbus_testing.engine import run_suite
from dbus_testing.model import IgnoreSpec, load_service
from dbus_testing.scanner import normalize_tree
from dbus_testing.scanner.draft import (
    ProbeResult,
    _slug,
    draft_service,
    draft_tests,
    split_loader_args,
)
from tests.conftest import run_cli
from tests.fixtures import fixture_path

MULTI = ("org.example.FxMultiA", "org.example.FxMultiB", "org.example.FxMultiC")


# --------------------------------------------------------------------------- 缺陷 3


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CanNTP", "can-ntp"),
        ("LocalRTC", "local-rtc"),
        ("NTPServer", "ntp-server"),
        ("Use24HourFormat", "use24-hour-format"),
        ("WeekBegins", "week-begins"),
        ("DSTOffset", "dst-offset"),
        ("Query", "query"),
        ("GetManagedObjects", "get-managed-objects"),
    ],
)
def test_slug_handles_acronyms(name: str, expected: str) -> None:
    """连续大写缩写不能被逐字母拆开。"""
    assert _slug(name) == expected


# --------------------------------------------------------------------------- 缺陷 1 / 5


def test_split_loader_args_recognizes_both_dash_forms() -> None:
    """Go 的 flag 包对 -enable 与 --enable 一视同仁,两种都要认。"""
    assert split_loader_args(("--enable", "timedate")) == ("timedate", ())
    assert split_loader_args(("-enable", "timedate")) == ("timedate", ())
    assert split_loader_args(("--enable=timedate",)) == ("timedate", ())
    assert split_loader_args(("-enable=timedate",)) == ("timedate", ())


def test_split_loader_args_strips_redundant_ignore_flag() -> None:
    """launcher 会自己补 -i,草稿里不能再留一份,否则 argv 出现 `-i -i`。"""
    assert split_loader_args(("--enable", "timedate", "-i")) == ("timedate", ())
    assert split_loader_args(("--enable", "timedate", "--ignore")) == ("timedate", ())
    # 识别不到 --enable 时不要乱剥别人的参数
    assert split_loader_args(("-i", "--verbose")) == (None, ("-i", "--verbose"))


def test_split_loader_args_keeps_unrelated_args() -> None:
    assert split_loader_args(("--enable", "timedate", "--loglevel", "debug")) == (
        "timedate",
        ("--loglevel", "debug"),
    )


def test_draft_service_persists_args() -> None:
    """探测时传的参数必须写进 service.yaml,否则 run 起来是另一个进程配置。"""
    probe = ProbeResult(
        services=("org.example.Demo",),
        binary=Path("/bin/demo"),
        binary_search=("/bin/demo",),
        args=("--loglevel", "debug"),
    )
    text = draft_service(probe)
    assert 'args: ["--loglevel", "debug"]' in text


def test_draft_service_converts_enable_to_go_loader() -> None:
    """`--enable X` 应转成规范的 kind: go-loader + module,而不是留一串裸 args。"""
    probe = ProbeResult(
        services=("org.deepin.dde.Timedate1",),
        binary=Path("/usr/libexec/deepin/dde-session-daemon"),
        binary_search=("/usr/libexec/deepin/dde-session-daemon",),
        args=("--enable", "timedate", "-i"),
    )
    text = draft_service(probe)
    assert "kind: go-loader" in text
    assert "module: timedate" in text
    assert "args:" not in text, "-i 会由 launcher 补,草稿里不该再出现"
    assert "kind: process" not in text


def test_go_loader_argv_has_no_duplicate_ignore_flag(tmp_path: Path) -> None:
    """端到端确认 argv 不再出现 `-i -i`。"""
    cfg = tmp_path / "service.yaml"
    cfg.write_text(
        """apiVersion: v1
services: [org.deepin.dde.Timedate1]
kind: go-loader
module: timedate
binary:
  search:
    - /bin/true
""",
        encoding="utf-8",
    )
    spec = load_service(cfg)
    argv = Launcher().build_argv(spec, Path("/bin/true"), "unix:x")
    assert argv == ["/bin/true", "--enable", "timedate", "-i"]
    assert argv.count("-i") == 1


# --------------------------------------------------------------------------- 缺陷 2


def test_draft_tests_emits_object_path_for_non_default_paths() -> None:
    """一个进程注册多个服务名/多个对象路径时,草稿必须写 object:,否则请求发错路径。"""
    tree = {
        "/org/example/Primary": """<node><interface name="org.example.Primary">
            <property name="Alpha" type="s" access="read"/></interface></node>""",
        "/org/example/Other": """<node><interface name="org.example.Other">
            <property name="Beta" type="i" access="read"/></interface></node>""",
    }
    normalized = normalize_tree(tree, IgnoreSpec())
    text, _ready, total = draft_tests(
        normalized,
        IgnoreSpec(),
        primary_service="org.example.Primary",
        primary_path="/org/example/Primary",
    )
    assert total == 2
    # 主路径主接口:两者都是默认值,不必写
    assert "get-prop: {property: Alpha}" in text
    # 非默认路径 + 非默认接口:两者都要显式写出
    assert "object: /org/example/Other" in text
    assert "interface: org.example.Other" in text


def test_draft_tests_skips_collapsed_pattern_paths() -> None:
    """归并后的动态子对象是模式而非真实路径,不能拿来发调用。"""
    tree = {
        "/org/example/Primary": """<node><interface name="org.example.Primary">
            <method name="Ping"><arg type="s" direction="out"/></method></interface></node>""",
        "/org/example/Primary/item/1": """<node><interface name="org.example.Item">
            <property name="Gamma" type="s" access="read"/></interface></node>""",
    }
    ignore = IgnoreSpec(paths=("/org/example/Primary/item/*",))
    normalized = normalize_tree(tree, ignore)
    assert 'collapsed="true"' in normalized, "前提:基线里确实有归并节点"
    text, _ready, _total = draft_tests(
        normalized, ignore, primary_service="org.example.Primary",
        primary_path="/org/example/Primary",
    )
    assert "item/*" not in text, "不应为模式路径生成用例"
    assert "Gamma" not in text


@pytest.mark.parametrize("fixture_name", ["fx_multi"])
def test_init_on_multi_service_process_runs_green(tmp_path: Path, fixture_name: str) -> None:
    """回归总闸:多服务名进程 init 出来的配置**不加修改**必须跑绿。

    这是缺陷 2 的端到端形态 —— 修复前这里会全线 InterfaceNotFound。
    """
    out = tmp_path / "gen"
    code, stdout, stderr = run_cli(
        ["init", "--binary", str(fixture_path(fixture_name)), "--out", str(out)]
    )
    assert code == 0, stdout + stderr
    service_yaml = (out / "service.yaml").read_text(encoding="utf-8")
    for name in MULTI:
        assert name in service_yaml, "三个服务名都应被探测到"

    tests_yaml = (out / "tests.yaml").read_text(encoding="utf-8")
    assert "object: /org/example/FxMulti" in tests_yaml
    assert "interface: org.example.FxMulti" in tests_yaml

    result = run_suite(out, include_static=False).result
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]


# --------------------------------------------------------------------------- 缺陷 4


def test_readonly_property_draft_uses_wide_assertion() -> None:
    """只读属性的错误名各实现不同(dbus-python 给 PropertyReadOnly,Go dbusutil 给 Failed),
    草稿必须先用宽断言,否则生成的用例在半数服务上必败。"""
    tree = {
        "/org/example/Demo": """<node><interface name="org.example.Demo">
            <property name="Fixed" type="s" access="read"/></interface></node>"""
    }
    normalized = normalize_tree(tree, IgnoreSpec())
    text, _ready, _total = draft_tests(
        normalized, IgnoreSpec(), primary_service="org.example.Demo",
        primary_path="/org/example/Demo",
    )
    assert "- name: fixed-readonly" in text
    assert 'expect: {error: "*"}' in text
    assert "PropertyReadOnly" not in text, "写死具体错误名会在 Go 服务上必败"


def test_init_on_readonly_fixture_runs_green(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    code, stdout, stderr = run_cli(
        ["init", "--binary", str(fixture_path("fx_readonly")), "--out", str(out)]
    )
    assert code == 0, stdout + stderr
    result = run_suite(out, include_static=False).result
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]


# --------------------------------------------------------------------------- 缺陷 6


def test_pytest_entry_point_name_is_stable() -> None:
    """entry point 名必须稳定为 dbus_testing。

    pytest 的 entry point 加载器按名字去重:同名的多份元数据(既装了 deb 又用
    PYTHONPATH 指源码树)会被安全跳过;改名则会在升级过程中把同一模块注册两次,
    触发 pluggy 的 "already registered under a different name"。
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    eps = data["project"]["entry-points"]["pytest11"]
    assert eps == {"dbus_testing": "dbus_testing.pytest_plugin"}


# --------------------------------------------------------------------------- 缺陷 1(参数入口)


def test_init_accepts_args_after_double_dash() -> None:
    """`--arg=--flag` 这种写法太别扭:`--` 之后的参数应原样透传。"""
    from dbus_testing.cli import build_parser

    ns = build_parser().parse_args(
        ["init", "--binary", "/bin/true", "--", "--enable", "timedate", "-i"]
    )
    assert ns.passthrough == ["--enable", "timedate", "-i"]


def test_init_still_accepts_repeated_arg_option() -> None:
    """兼容旧写法,且与 `--` 之后的参数合并(--arg 在前)。"""
    from dbus_testing.cli import build_parser

    ns = build_parser().parse_args(
        ["init", "--binary", "/bin/true", "--arg=--loglevel", "--arg=debug", "--", "-i"]
    )
    merged = tuple(ns.arg or ()) + tuple(ns.passthrough or ())
    assert merged == ("--loglevel", "debug", "-i")
