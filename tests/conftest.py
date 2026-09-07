"""框架自测公共设施:用 fixture 服务在临时目录里生成 tests/dbus 配置。

自测不依赖 DDE 环境,只需 dbus-daemon + dbus-python + PyGObject。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import fixture_path

REQUIRES = ("dbus-daemon",)


def pytest_report_header(config: pytest.Config) -> str:
    missing = [b for b in REQUIRES if shutil.which(b) is None]
    return f"dbus-testing 自测环境: {'缺少 ' + ', '.join(missing) if missing else '就绪'}"


@pytest.fixture(scope="session", autouse=True)
def _require_bus() -> None:
    if shutil.which("dbus-daemon") is None:
        pytest.skip("需要 dbus-daemon", allow_module_level=True)


def make_suite(
    tmp_path: Path,
    fixture: str,
    *,
    services: tuple[str, ...],
    cases: str = "",
    contract: str | None = None,
    extra_service: str = "",
    ready_timeout: str = "10s",
    mode: str = "isolate",
) -> Path:
    """在 tmp_path 下生成一个可直接跑的 tests/dbus 目录。"""
    suite = tmp_path / "dbus"
    suite.mkdir(parents=True, exist_ok=True)
    names = "\n".join(f"  - {s}" for s in services)
    ready = "\n".join(f"    - {s}" for s in services)
    service_yaml = f"""apiVersion: v1
process: {fixture}
mode: {mode}
kind: process
services:
{names}
binary:
  search:
    - {fixture_path(fixture)}
sandbox:
  home: tmp
ready:
  name-owner:
{ready}
  timeout: {ready_timeout}
{extra_service}"""
    (suite / "service.yaml").write_text(service_yaml, encoding="utf-8")
    if cases:
        (suite / "tests.yaml").write_text(cases, encoding="utf-8")
    if contract is not None:
        (suite / "contract.xml").write_text(contract, encoding="utf-8")
    return suite


def run_cli(args: list[str]) -> tuple[int, str, str]:
    """在进程内跑 CLI,捕获 stdout/stderr 与退出码。"""
    import contextlib
    import io

    from dbus_testing.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(args)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def suite_factory(tmp_path: Path) -> Any:
    def _make(fixture: str, **kwargs: Any) -> Path:
        return make_suite(tmp_path, fixture, **kwargs)

    return _make
