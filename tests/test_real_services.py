"""真实 DDE 服务的集成验证(默认按需跳过,需本机装有对应服务)。

这些用例把"框架能测真实 DDE 服务"这件事固定下来,防止回归。
运行:  python3 -m pytest tests/test_real_services.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dbus_testing.engine import run_suite
from dbus_testing.scanner import diff

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _require(binary: str) -> None:
    if not Path(binary).exists():
        pytest.skip(f"本机没有 {binary}")


def _run(example: str) -> object:
    suite = EXAMPLES / example
    assert (suite / "service.yaml").exists(), f"缺少样板 {suite}"
    return run_suite(suite, include_static=False).result


@pytest.mark.needs_dde
def test_pinyin1_t1_isolate() -> None:
    """T1:独立 Go 二进制,私有总线密闭,含真实返回值断言。"""
    _require("/usr/lib/deepin-api/hans2pinyin")
    result = _run("pinyin1")
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]
    assert result.total >= 4


@pytest.mark.needs_dde
def test_graphic1_t1_isolate() -> None:
    _require("/usr/lib/deepin-api/graphic")
    result = _run("graphic1")
    assert result.failed == 0 and result.errored == 0, [
        (c.name, c.status, c.detail) for c in result.cases
    ]


@pytest.mark.needs_dde
def test_application_manager1_t3_isolate_with_systemd_mock() -> None:
    """T3:强依赖 systemd 的服务,靠 systemd_dde mock 前置后密闭跑通。"""
    _require("/usr/bin/dde-application-manager")
    pytest.importorskip("dbusmock")
    result = _run("application-manager1")
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]
    # 契约用例必须真的比对过基线
    assert any(c.name == "contract" and c.status == "pass" for c in result.cases)


@pytest.mark.needs_dde
def test_systeminfo1_t2_go_loader(tmp_path: Path) -> None:
    """T2:loader 模块形态(dde-session-daemon --enable systeminfo)。"""
    daemon = "/usr/libexec/deepin/dde-session-daemon"
    _require(daemon)
    suite = tmp_path / "dbus"
    suite.mkdir()
    (suite / "service.yaml").write_text(
        f"""apiVersion: v1
process: dde-session-daemon
mode: isolate
kind: go-loader
tier: T2
module: systeminfo
services:
  - org.deepin.dde.SystemInfo1
binary:
  search:
    - {daemon}
sandbox:
  home: tmp
  env:
    QT_QPA_PLATFORM: offscreen
ready:
  name-owner:
    - org.deepin.dde.SystemInfo1
  timeout: 25s
ignore:
  interfaces:
    - "org.freedesktop.DBus.*"
""",
        encoding="utf-8",
    )
    (suite / "tests.yaml").write_text(
        """apiVersion: v1
cases:
  - name: version-readable
    get-prop: {property: Version}
    expect: {type: s}
  - name: distro-id-nonempty
    get-prop: {property: DistroID}
    expect: {type: s, nonempty: true}
""",
        encoding="utf-8",
    )
    result = run_suite(suite, include_static=False).result
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]


@pytest.mark.needs_dde
def test_dsm_plugin_via_host(tmp_path: Path) -> None:
    """T3:DSM 插件经宿主替身密闭注册(需安装可选组件 dsm-host)。"""
    plugin = "/usr/lib/x86_64-linux-gnu/deepin-service-manager/libplugin-dde-appearance.so"
    _require(plugin)
    if shutil.which("dbus-testing-dsm-host") is None and not Path(
        "/usr/libexec/dbus-testing/dbus-testing-dsm-host"
    ).exists():
        pytest.skip("未安装可选组件 dbus-testing-dsm-host")
    suite = tmp_path / "dbus"
    suite.mkdir()
    (suite / "service.yaml").write_text(
        f"""apiVersion: v1
process: dsm-host
mode: isolate
kind: dsm
tier: T3
services:
  - org.deepin.dde.Appearance1
plugin: {plugin}
sandbox:
  home: tmp
  env:
    QT_QPA_PLATFORM: offscreen
ready:
  name-owner:
    - org.deepin.dde.Appearance1
  timeout: 25s
ignore:
  interfaces:
    - "org.freedesktop.DBus.*"
""",
        encoding="utf-8",
    )
    (suite / "tests.yaml").write_text(
        """apiVersion: v1
cases:
  - name: font-size-readable
    get-prop: {property: FontSize}
    expect: {type: d}
""",
        encoding="utf-8",
    )
    result = run_suite(suite, include_static=False).result
    assert result.ok, [(c.name, c.status, c.detail) for c in result.cases]


@pytest.mark.needs_dde
def test_attach_mode_readonly_contract_on_real_session() -> None:
    """attach 模式:只读 introspect 真实会话中运行的服务,不做任何调用。"""
    import os

    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        pytest.skip("需要真实会话总线")
    from dbus_testing.core.bus import BusType, attach_bus_env
    from dbus_testing.core.client import BusClient
    from dbus_testing.model import IgnoreSpec
    from dbus_testing.scanner import normalize_tree
    from dbus_testing.scanner.introspect import snapshot

    env = attach_bus_env(BusType.SESSION)
    client = BusClient(env[BusType.SESSION.env_name])
    service = "org.desktopspec.ApplicationManager1"
    if not client.name_has_owner(service):
        pytest.skip(f"{service} 未在当前会话中运行")
    ignore = IgnoreSpec(paths=("/org/desktopspec/ApplicationManager1/*",))
    first = normalize_tree(snapshot(client, (service,), ignore), ignore)
    second = normalize_tree(snapshot(client, (service,), ignore), ignore)
    assert first == second, "同一服务两次快照应逐字节一致"
    assert diff(first, second) == []
    assert 'collapsed="true"' in first, "动态子对象应被归并"
    client.close()
