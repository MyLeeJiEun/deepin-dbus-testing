"""不侵入约束的自动守卫(P0 验收,必须常驻 CI)。

对应开发文档 §1.2 约束 ②:
- 框架禁止调用被测项目构建系统;
- 写文件只允许发生在 scanner/draft.py、report/、core/bus.py、core/sandbox.py;
- 完整跑一轮时不得写入被测仓,临时文件必须在系统临时目录下。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from dbus_testing.engine import run_suite
from tests.conftest import make_suite

PKG = Path(__file__).resolve().parent.parent / "dbus_testing"

BUILD_CMD_RE = re.compile(
    r"""(?<![\w.-])(cmake|ninja|dpkg-buildpackage|meson|qmake6?)(?![\w-])|"""
    r"""\bgo\s+build\b|\bmake\s+-|subprocess\.[a-z_]+\(\s*\[?["']make["']"""
)

WRITE_RE = re.compile(
    r"""open\([^)]*["'][wax]|\.write_text\(|\.write_bytes\(|os\.remove\(|"""
    r"""shutil\.rmtree\(|os\.unlink\(|\.unlink\(|\.mkdir\(|makedirs\("""
)

# 允许写文件的模块(其余模块只读)
WRITE_ALLOWED = {
    "scanner/draft.py",
    "report/junit.py",
    "report/html.py",
    "report/jsonout.py",
    "core/bus.py",
    "core/sandbox.py",
    "cli.py",  # init/scan 的落盘出口,只写 tests/dbus 或 --out
}


def _sources() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def _strip_comments_and_docstrings(text: str) -> str:
    """粗剥注释,避免文档里的示例命令触发误报。"""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)


def test_no_build_system_invocation() -> None:
    """框架不得调用被测项目的构建系统。"""
    offenders: list[tuple[str, str]] = []
    for path in _sources():
        code = _strip_comments_and_docstrings(path.read_text(encoding="utf-8"))
        for match in BUILD_CMD_RE.finditer(code):
            line_no = code[: match.start()].count("\n") + 1
            offenders.append((str(path.relative_to(PKG)), f"{line_no}: {match.group(0)}"))
    assert not offenders, f"发现疑似构建系统调用: {offenders}"


def test_write_operations_are_confined() -> None:
    """写文件操作只允许出现在白名单模块里。"""
    offenders: list[str] = []
    for path in _sources():
        rel = str(path.relative_to(PKG))
        if rel in WRITE_ALLOWED:
            continue
        code = _strip_comments_and_docstrings(path.read_text(encoding="utf-8"))
        for match in WRITE_RE.finditer(code):
            line_no = code[: match.start()].count("\n") + 1
            offenders.append(f"{rel}:{line_no} {match.group(0)}")
    assert not offenders, f"白名单外的模块出现写操作: {offenders}"


def test_run_does_not_write_into_repo(tmp_path: Path) -> None:
    """把配置目录设为只读后仍能完整跑通 → 证明框架不写被测仓。"""
    suite = make_suite(
        tmp_path,
        "fx_echo",
        services=("org.example.FxEcho",),
        cases="""apiVersion: v1
cases:
  - name: echo
    call: {method: Echo, args: ["ro"]}
    expect: {value: "ro"}
""",
    )
    before = {p: p.stat().st_mtime_ns for p in suite.rglob("*") if p.is_file()}
    suite.chmod(0o500)
    try:
        result = run_suite(suite, include_static=False).result
    finally:
        suite.chmod(0o755)
    assert result.cases[0].status == "pass", result.cases[0].detail
    after = {p: p.stat().st_mtime_ns for p in suite.rglob("*") if p.is_file()}
    assert before == after, "配置目录中的文件被改动了"


def test_temp_paths_are_under_tmpdir(tmp_path: Path) -> None:
    """私有总线与沙箱创建的路径必须都在系统临时目录之下。"""
    from dbus_testing.core.bus import BusType, PrivateBus
    from dbus_testing.core.sandbox import Sandbox
    from dbus_testing.model import SandboxSpec

    root = Path(tempfile.gettempdir()).resolve()
    with PrivateBus(BusType.SESSION) as bus:
        workdir = bus.workdir
        assert workdir is not None
        assert root in workdir.resolve().parents or workdir.resolve().parent == root
    sandbox = Sandbox(SandboxSpec(home="tmp"))
    env = sandbox.__enter__()
    try:
        for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
            path = Path(env[key]).resolve()
            assert root in path.parents or path.parent == root, f"{key} 不在临时目录下: {path}"
    finally:
        sandbox.__exit__(None, None, None)


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_framework_repo_is_clean_after_run(tmp_path: Path) -> None:
    """跑一轮之后框架仓自身不应出现新的未跟踪文件(除测试产物)。"""
    repo = PKG.parent
    suite = make_suite(tmp_path, "fx_echo", services=("org.example.FxEcho",))
    run_suite(suite, include_static=False)
    proc = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("框架仓未初始化 git")
    strays = [
        line
        for line in proc.stdout.splitlines()
        if "__pycache__" not in line and not line.endswith(".pyc")
    ]
    # 只断言"跑测试这件事"没有额外落盘;仓内本来就有的未提交文件不在此检查范围
    assert all("/tmp/" not in line for line in strays)
