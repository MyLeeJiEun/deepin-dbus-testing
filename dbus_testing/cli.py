"""dbus-testing 命令行入口。

子命令:
  init   探测服务并生成 tests/dbus/ 骨架与用例草稿
  scan   运行时 introspect,生成/更新 contract.xml
  check  契约校验(--static:源码声明 vs 基线,不起服务)
  run    执行声明式用例并产出报告
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .core.bus import BusType, PrivateBus, attach_bus_env
from .core.client import BusClient
from .core.diagnose import diagnose
from .core.launcher import Launcher
from .core.sandbox import Sandbox
from .engine import (
    CONTRACT_FILENAME,
    SERVICE_FILENAME,
    TESTS_FILENAME,
    Engine,
    Session,
    build_coverage,
    load_baseline,
    run_suite,
)
from .errors import (
    EXIT_FAILED,
    EXIT_INTERNAL,
    EXIT_OK,
    BinaryNotFound,
    ConfigError,
    DbusTestingError,
    ProcessDied,
)
from .model import Case, IgnoreSpec, SandboxSpec, load_service, service_name_to_path
from .report import write_html, write_json, write_junit
from .results import CaseResult, RunResult
from .scanner.draft import ProbeResult, draft_service, draft_tests
from .scanner.diff import diff
from .scanner.introspect import snapshot
from .scanner.normalize import normalize_tree
from .scanner.srcxml import src_baseline_xml, static_diff

log = logging.getLogger("dbus_testing")

DISPLAY_KEYS = ("platform plugin", "could not connect to display", "xcb")
LIVE_CONTRACT_FILENAME = "contract-live.xml"


# --------------------------------------------------------------------------- 通用


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s",
        stream=sys.stderr,
    )


def _emit(path: Path, text: str, *, force: bool) -> bool:
    """写文件;已存在且未 --force 时跳过。返回是否写入。"""
    if path.exists() and not force:
        print(f"  跳过(已存在,加 --force 覆盖): {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  写入: {path}")
    return True


def _print_summary(result: RunResult) -> None:
    print()
    print("=" * 68)
    for case in result.cases:
        mark = {
            "pass": "PASS",
            "fail": "FAIL",
            "error": "ERROR",
            "config-error": "CONFIG",
            "skip": "SKIP",
        }[case.status]
        extra = " (flaky)" if case.flaky else ""
        print(f"  [{mark:6}] {case.name}{extra}  {case.elapsed * 1000:.0f}ms")
        if case.status in ("fail", "error", "config-error"):
            if case.code:
                print(f"           {case.code}: {case.summary}")
            for line in (case.detail or "").splitlines():
                print(f"           {line}")
            if case.hint:
                print(f"           提示: {case.hint}")
        elif case.status == "skip" and case.summary:
            print(f"           {case.summary}")
    print("-" * 68)
    print(
        f"  合计 {result.total} 例:通过 {result.passed}、失败 {result.failed}、"
        f"错误 {result.errored}、配置错误 {result.config_errors}、跳过 {result.skipped}"
        f",耗时 {result.elapsed:.2f}s"
    )
    if result.coverage.total:
        cov = result.coverage
        print(
            f"  接口覆盖:声明一致 {cov.declared_ok}/{cov.total}、"
            f"被用例执行 {cov.executed_count}/{cov.total}"
        )
    if result.static_skipped:
        print(f"  静态契约校验跳过:{result.static_skipped}")
    print("=" * 68)


def _write_reports(result: RunResult, out: Path | None) -> None:
    if out is None:
        return
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_junit(result, out / "junit.xml")
    write_json(result, out / "results.json")
    write_html(result, out / "report.html")
    if result.live_xml:
        (out / LIVE_CONTRACT_FILENAME).write_text(result.live_xml, encoding="utf-8")
    print(f"  报告已产出: {out}")


def _synthetic_case(name: str, status: str, *, code: str = "", summary: str = "", detail: str = "",
                    hint: str = "") -> CaseResult:
    """为无用例但需在 CI 里可见的检查(如 check)合成一个 testcase。"""
    case = Case(name=name, steps=())
    return CaseResult(
        case=case,
        status=status,  # type: ignore[arg-type]
        code=code or None,
        summary=summary,
        detail=detail,
        hint=hint,
    )


def _keep_hint(session: Session) -> None:
    if session.bus is not None:
        print()
        print("  --keep-bus:环境已保留,手工排障:")
        print(f'    export {BusType.SESSION.env_name}="{session.bus.address}"')
        if session.system_bus is not None:
            print(f'    export {BusType.SYSTEM.env_name}="{session.system_bus.address}"')
        if session.sandbox is not None and session.sandbox.home is not None:
            print(f'    export HOME="{session.sandbox.home}"')
        print(f"    busctl --address=\"{session.bus.address}\" list --acquired")


def _open_shell(env: dict[str, str]) -> None:
    shell = os.environ.get("SHELL", "/bin/sh")
    child = dict(os.environ)
    child.update(env)
    child["PS1"] = "(dbus-testing) $ "
    print(f"  进入调试 shell({shell}),exit 退出")
    subprocess.call([shell], env=child)  # noqa: S603 - 用户显式请求


# --------------------------------------------------------------------------- init


def _probe_from_binary(
    binary: Path, args: tuple[str, ...], timeout: float, build_dir: Path | None
) -> tuple[ProbeResult, dict[str, str]]:
    """在私有总线上拉起二进制,探测它注册了哪些服务名。

    失败时按诊断规则重试(如自动补 QT_QPA_PLATFORM=offscreen),并把生效配置写回草稿。
    """
    from .model import ReadySpec, ServiceSpec

    attempts: list[dict[str, str]] = [{}, {"QT_QPA_PLATFORM": "offscreen"}]
    notes: list[str] = []
    last_output = ""
    for extra in attempts:
        with PrivateBus(BusType.SESSION) as bus:
            client = BusClient(bus.address)
            before = set(client.list_acquired_names())
            sandbox = Sandbox(SandboxSpec(home="tmp", env=extra))
            sandbox_env = sandbox.__enter__()
            overlay = dict(bus.env)
            overlay.update(sandbox_env)
            spec = ServiceSpec(
                api_version="v1",
                process=binary.name,
                services=("org.example.Probe",),
                binary_search=(str(binary),),
                args=args,
                ready=ReadySpec(name_owner=(), timeout=timeout),
            )
            handle = Launcher().launch(spec, bus.address, overlay)
            deadline = time.monotonic() + timeout
            found: list[str] = []
            while time.monotonic() < deadline:
                if not handle.is_alive():
                    break
                now = set(client.list_acquired_names())
                new = sorted(n for n in now - before if n != "org.freedesktop.DBus")
                if new:
                    time.sleep(0.3)  # 再等一拍,捕获同进程注册的其它名字
                    now = set(client.list_acquired_names())
                    found = sorted(n for n in now - before if n != "org.freedesktop.DBus")
                    break
                time.sleep(0.1)

            if found:
                tree = snapshot(client, tuple(found), IgnoreSpec())
                if extra:
                    notes.append(f"自动补充环境变量生效: {extra}")
                probe = ProbeResult(
                    services=tuple(found),
                    binary=binary,
                    binary_search=_binary_candidates(binary, build_dir),
                    env=dict(extra),
                    notes=notes,
                    args=args,
                )
                handle.terminate()
                sandbox.__exit__(None, None, None)
                client.close()
                return probe, tree

            last_output = handle.output_tail(20)
            handle.terminate()
            sandbox.__exit__(None, None, None)
            client.close()

        lowered = last_output.lower()
        if not any(k in lowered for k in DISPLAY_KEYS):
            break
        notes.append("首次拉起失败疑似缺少显示环境,已重试 QT_QPA_PLATFORM=offscreen")

    raise ProcessDied(
        "init 未能探测到该二进制注册的任何服务名",
        output=last_output,
        argv=[str(binary), *args],
    )


def _binary_candidates(binary: Path, build_dir: Path | None) -> tuple[str, ...]:
    """构建产物优先的候选列表;把 --build-dir 前缀替换回 ${BUILD_DIR} 便于跨机复用。"""
    out: list[str] = []
    resolved = binary.resolve()
    if build_dir is not None:
        bd = Path(build_dir).resolve()
        try:
            rel = resolved.relative_to(bd)
            out.append(f"${{BUILD_DIR}}/{rel}")
        except ValueError:
            out.append(f"${{BUILD_DIR}}/{binary.name}")
            out.append(f"${{BUILD_DIR}}/bin/{binary.name}")
    else:
        out.append(f"${{BUILD_DIR}}/{binary.name}")
        out.append(f"${{BUILD_DIR}}/bin/{binary.name}")
    if str(resolved) not in out:
        out.append(str(resolved))
    return tuple(out)


def cmd_init(ns: argparse.Namespace) -> int:
    out_dir = Path(ns.out)
    build_dir = Path(ns.build_dir).resolve() if ns.build_dir else None

    if ns.from_running:
        env = attach_bus_env(BusType.SESSION)
        client = BusClient(env[BusType.SESSION.env_name])
        service = ns.from_running
        if not client.name_has_owner(service):
            raise ConfigError(
                f"{service} 未在当前会话总线上运行;请先启动它,或改用 --binary 指向构建产物"
            )
        tree = snapshot(client, (service,), IgnoreSpec())
        probe = ProbeResult(
            services=(service,),
            mode="attach",
            notes=[
                "由 --from-running 生成:mode=attach 仅做只读契约校验",
                "补齐 binary.search 后可切换 mode: isolate 做密闭测试",
            ],
        )
        client.close()
    else:
        binary = Path(ns.binary)
        if not binary.exists():
            raise BinaryNotFound(
                f"--binary 指向的文件不存在: {binary}", candidates=[(str(binary), "不存在")]
            )
        extra_args = tuple(ns.arg or ()) + tuple(getattr(ns, "passthrough", None) or ())
        probe, tree = _probe_from_binary(
            binary, extra_args, float(ns.ready_timeout), build_dir
        )

    ignore = IgnoreSpec()
    contract = normalize_tree(tree, ignore)
    primary = probe.services[0]
    tests_text, ready, total = draft_tests(
        contract, ignore, primary_service=primary, primary_path=service_name_to_path(primary)
    )

    print(f"探测结果:服务名 {', '.join(probe.services)}")
    print(f"生成到 {out_dir}:")
    _emit(out_dir / SERVICE_FILENAME, draft_service(probe), force=ns.force)
    _emit(out_dir / CONTRACT_FILENAME, contract, force=ns.force)
    _emit(out_dir / TESTS_FILENAME, tests_text, force=ns.force)
    extra = out_dir / "test_extra.py"
    _emit(
        extra,
        '"""可选逃生舱:有外部副作用、无法用六原语声明的行为放这里。\n\n'
        "示例:\n"
        "    def test_launch_app(dbus_service):\n"
        '        dbus_service.call("Launch", "demo.desktop")\n'
        '        # 断言外部副作用(进程/文件/窗口)\n'
        '"""\n',
        force=ns.force,
    )
    if total:
        print(f"用例草稿:{ready} 条可直接运行 / 成员总数 {total}(其余为注释态待补参数)")
    print()
    print("下一步:")
    print(f"  1) 审阅 {out_dir / CONTRACT_FILENAME} 后提交入仓")
    if build_dir:
        print(f"  2) dbus-testing run {out_dir} --build-dir {build_dir}")
    else:
        print(f"  2) dbus-testing run {out_dir} --build-dir <构建目录>")
    return EXIT_OK


# --------------------------------------------------------------------------- scan


def cmd_scan(ns: argparse.Namespace) -> int:
    tests_dir = Path(ns.dir)
    spec = load_service(
        tests_dir / SERVICE_FILENAME,
        build_dir=Path(ns.build_dir).resolve() if ns.build_dir else None,
    )
    with Session(spec, keep=ns.keep_bus) as session:
        assert session.client is not None
        tree = snapshot(session.client, spec.services, spec.ignore)
        contract = normalize_tree(tree, spec.ignore)
        if ns.keep_bus:
            _keep_hint(session)
    print(f"运行时快照:{len(tree)} 个对象,{contract.count('<interface')} 个接口声明")
    if ns.emit:
        target = tests_dir / CONTRACT_FILENAME
        _emit(target, contract, force=True)
        print("  请人工审阅 diff 后提交(contract.xml 即接口 API review 载体)")
    else:
        sys.stdout.write(contract)
    return EXIT_OK


# --------------------------------------------------------------------------- check


def cmd_check(ns: argparse.Namespace) -> int:
    tests_dir = Path(ns.dir)
    spec = load_service(
        tests_dir / SERVICE_FILENAME,
        build_dir=Path(ns.build_dir).resolve() if ns.build_dir else None,
    )
    baseline = load_baseline(tests_dir)
    if not baseline:
        raise ConfigError(
            f"{tests_dir / CONTRACT_FILENAME} 不存在;先跑 `dbus-testing scan {tests_dir} --emit`"
        )
    result = RunResult()

    if ns.static:
        src = src_baseline_xml(spec.repo_root, spec.src_xml_globs, spec.ignore)
        if src is None:
            result.static_skipped = (
                f"仓内未找到源码声明 XML(src-xml-globs={list(spec.src_xml_globs)})"
            )
            result.cases.append(
                _synthetic_case(
                    "contract-static", "skip", summary=result.static_skipped
                )
            )
            print(f"SKIP 静态契约校验:{result.static_skipped}")
        else:
            deltas = static_diff(src, baseline)
            result.static_contract = deltas
            if deltas:
                detail = "\n".join(f"  {d.render()}" for d in deltas)
                result.cases.append(
                    _synthetic_case(
                        "contract-static",
                        "fail",
                        code="E_CONTRACT_DRIFT",
                        summary=f"源码声明与 contract.xml 有 {len(deltas)} 处差异",
                        detail=detail,
                        hint="接口有意变更 → 更新 contract.xml;否则检查源码声明 XML 是否漏改",
                    )
                )
                print(f"FAIL 源码声明与基线有 {len(deltas)} 处差异:")
                print("  (方向:before=仓内源码声明,after=contract.xml 基线;")
                print("   removed=源码有而基线缺,added=基线有而源码缺)")
                print(detail)
            else:
                result.cases.append(_synthetic_case("contract-static", "pass"))
                print("PASS 源码声明与 contract.xml 一致")
        result.coverage = build_coverage(baseline, "", src, [])
    else:
        with Session(spec, keep=ns.keep_bus) as session:
            assert session.client is not None
            tree = snapshot(session.client, spec.services, spec.ignore)
            actual = normalize_tree(tree, spec.ignore)
            result.live_xml = actual
            result.env = session.env_fingerprint()
            deltas = diff(baseline, actual)
            result.contract = list(deltas)
            if deltas:
                detail = "\n".join(f"  {d.render()}" for d in deltas)
                result.cases.append(
                    _synthetic_case(
                        "contract-live",
                        "fail",
                        code="E_CONTRACT_DRIFT",
                        summary=f"运行时接口与 contract.xml 有 {len(deltas)} 处差异",
                        detail=detail,
                        hint="接口有意变更 → `scan --emit` 更新基线并 review;否则这是接口回归",
                    )
                )
                print(f"FAIL 运行时接口与基线有 {len(deltas)} 处差异:")
                print(detail)
            else:
                result.cases.append(_synthetic_case("contract-live", "pass"))
                print("PASS 运行时接口与 contract.xml 一致")
            if ns.keep_bus:
                _keep_hint(session)
        result.coverage = build_coverage(baseline, result.live_xml, None, [])

    _write_reports(result, Path(ns.report) if ns.report else None)
    return EXIT_OK if result.ok else EXIT_FAILED


# --------------------------------------------------------------------------- run


def cmd_run(ns: argparse.Namespace) -> int:
    outcome = run_suite(
        Path(ns.dir),
        build_dir=Path(ns.build_dir).resolve() if ns.build_dir else None,
        keep=ns.keep_bus or ns.shell,
        timeout_scale=float(ns.timeout_scale),
        case_filter=ns.k,
        include_static=not ns.no_static,
    )
    result = outcome.result
    if not result.cases:
        print("警告:未找到任何用例(tests.yaml 缺失或被 -k 过滤掉),仅做了契约快照")
    _print_summary(result)
    _write_reports(result, Path(ns.report) if ns.report else None)
    if ns.shell:
        _open_shell({BusType.SESSION.env_name: result.env.get("bus_address", "")})
    return EXIT_OK if result.ok else EXIT_FAILED


# --------------------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbus-testing",
        description="DDE DBus 接口自动化测试框架(配置驱动,语言无关,不侵入业务代码)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", help="打印总线地址、解析到的二进制等调试信息")
    common.add_argument("--keep-bus", action="store_true", help="跑完保留总线与沙箱供手工排障")
    common.add_argument("--build-dir", help="构建目录,供 ${BUILD_DIR} 展开")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", parents=[common], help="生成 tests/dbus/ 骨架与用例草稿")
    src = p_init.add_mutually_exclusive_group(required=True)
    src.add_argument("--binary", help="被测二进制路径(通常在构建目录下)")
    src.add_argument("--from-running", help="对当前会话中已运行的服务名反向生成(mode=attach)")
    p_init.add_argument("--out", default="tests/dbus", help="输出目录(默认 tests/dbus)")
    p_init.add_argument(
        "--arg",
        action="append",
        help="传给被测二进制的额外参数,可重复;含横线时用 --arg=--flag 形式。"
        "更省事的写法是把它们放在 `--` 之后:init --binary X -- --enable timedate",
    )
    p_init.add_argument(
        "passthrough",
        nargs="*",
        help=argparse.SUPPRESS,  # `--` 之后的参数原样传给被测二进制
    )
    p_init.add_argument("--ready-timeout", default=10.0, type=float, help="探测等待秒数")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_init.set_defaults(func=cmd_init)

    p_scan = sub.add_parser("scan", parents=[common], help="运行时 introspect 并生成 contract.xml")
    p_scan.add_argument("dir", help="tests/dbus 目录")
    p_scan.add_argument("--emit", action="store_true", help="写入 contract.xml(否则打印到 stdout)")
    p_scan.set_defaults(func=cmd_scan)

    p_check = sub.add_parser("check", parents=[common], help="契约校验")
    p_check.add_argument("dir", help="tests/dbus 目录")
    p_check.add_argument(
        "--static", action="store_true", help="源码声明 XML vs 基线(不起服务,秒级)"
    )
    p_check.add_argument("--report", help="报告输出目录")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", parents=[common], help="执行声明式用例并产出报告")
    p_run.add_argument("dir", help="tests/dbus 目录")
    p_run.add_argument("--report", help="报告输出目录")
    p_run.add_argument("-k", help="只跑名字包含该子串的用例")
    p_run.add_argument("--timeout-scale", default=1.0, type=float, help="统一放大所有超时")
    p_run.add_argument("--shell", action="store_true", help="跑完进入带总线环境变量的调试 shell")
    p_run.add_argument("--no-static", action="store_true", help="跳过源码声明比对")
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    _setup_logging(bool(getattr(ns, "verbose", False)))
    if shutil.which("dbus-daemon") is None:
        print("警告:未找到 dbus-daemon,isolate 模式不可用(请安装 dbus 包)", file=sys.stderr)
    try:
        return int(ns.func(ns))
    except DbusTestingError as exc:
        d = diagnose(exc)
        print(d.render(), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # pragma: no cover - 兜底
        d = diagnose(exc)
        print(d.render(), file=sys.stderr)
        if getattr(ns, "verbose", False):
            raise
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
