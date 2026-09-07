"""错误分类与可操作提示.

开发纪律:所有面向用户的失败都必须经这里出口,给出错误码 + detail + hint;
裸 traceback 视为缺陷。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import (
    AssertionFailed,
    BinaryNotFound,
    BusError,
    CallTimeout,
    ConfigError,
    ContractDrift,
    DbusTestingError,
    GuardDenied,
    MockUnfaithful,
    ProcessDied,
    ReadyTimeout,
    SignalTimeout,
)

# 从服务输出中识别成因的关键字
_DISPLAY_HINTS = (
    "platform plugin",
    "could not connect to display",
    "xcb",
    "wayland",
    "QT_QPA_PLATFORM",
)
_DEP_HINTS = (
    "org.freedesktop.systemd1",
    "org.desktopspec.ConfigManager",
    "org.freedesktop.login1",
    "ServiceUnknown",
    "NameHasNoOwner",
)


@dataclass(frozen=True)
class Diagnosis:
    code: str
    summary: str
    detail: str = ""
    hint: str = ""

    def render(self) -> str:
        out = [f"[{self.code}] {self.summary}"]
        if self.detail:
            out.append(self.detail.rstrip())
        if self.hint:
            out.append(f"提示: {self.hint}")
        return "\n".join(out)


def _ctx(exc: DbusTestingError, key: str, default: Any = None) -> Any:
    return exc.context.get(key, default)


def diagnose(exc: BaseException) -> Diagnosis:
    """把异常翻译成带可操作提示的诊断。"""
    if isinstance(exc, BinaryNotFound):
        build_dir = _ctx(exc, "build_dir")
        hint = "检查 --build-dir 是否指向正确的构建目录,或先执行构建;候选来自 service.yaml 的 binary.search"
        if not build_dir:
            hint += "。当前未传 --build-dir,${BUILD_DIR} 候选会被跳过"
        return Diagnosis(exc.code, "未找到被测二进制", exc.message, hint)

    if isinstance(exc, ProcessDied):
        output = str(_ctx(exc, "output", "") or "")
        lowered = output.lower()
        detail_parts = []
        if _ctx(exc, "exit_code") is not None:
            detail_parts.append(f"退出码: {_ctx(exc, 'exit_code')}")
        if _ctx(exc, "argv"):
            detail_parts.append("命令: " + " ".join(_ctx(exc, "argv")))
        if output:
            detail_parts.append("进程输出(尾部):\n" + output)
        detail = "\n".join(detail_parts)
        if any(k.lower() in lowered for k in _DISPLAY_HINTS):
            hint = (
                "该服务是 GUI 程序,无显示环境起不来:在 service.yaml 的 "
                "sandbox.env 里加 QT_QPA_PLATFORM=offscreen"
            )
        elif any(k.lower() in lowered for k in _DEP_HINTS):
            hint = (
                "服务缺少外部依赖即退出:在 service.yaml 的 needs: 里前置对应 mock"
                "(如 - mock: systemd / - mock: org.desktopspec.ConfigManager),"
                "mock 必须在拉起被测进程之前启动"
            )
        else:
            hint = "用 --keep-bus 保留总线与沙箱后手工复现,或用 --verbose 查看完整命令与环境"
        return Diagnosis(exc.code, "被测进程在就绪前退出", detail, hint)

    if isinstance(exc, ReadyTimeout):
        missing = _ctx(exc, "missing", []) or []
        acquired = _ctx(exc, "acquired", []) or []
        output = str(_ctx(exc, "output", "") or "")
        detail = (
            f"期望的服务名: {', '.join(_ctx(exc, 'expected', []) or [])}\n"
            f"仍未出现: {', '.join(missing)}\n"
            f"总线上实际已注册: {', '.join(acquired) if acquired else '(无)'}"
        )
        if output:
            detail += "\n进程输出(尾部):\n" + output
        others = [n for n in acquired if n not in ("org.freedesktop.DBus",)]
        if others:
            hint = (
                "总线上有其它名字,通常是 services:/ready.name-owner 写错了。"
                f"把它改成实际注册的名字,例如: {others[0]}"
            )
        else:
            hint = (
                "总线上没有任何业务名字:服务未注册成功。按 E_PROC_DIED 排查"
                "(依赖 mock、离屏平台),或适当调大 ready.timeout"
            )
        return Diagnosis(exc.code, "等待服务注册超时", detail, hint)

    if isinstance(exc, MockUnfaithful):
        missing = _ctx(exc, "missing", []) or []
        listed = "\n".join(f"  - {m}" for m in missing)
        spawn_error = _ctx(exc, "spawn_error")
        if spawn_error:
            listed = f"  启动失败原因: {spawn_error}" + (f"\n{listed}" if listed else "")
        snippet = ""
        if missing:
            iface, _, method = str(missing[0]).rpartition(".")
            snippet = (
                "\n模板片段示例(dbusmock):\n"
                f"    dbus_object.AddMethod('{iface}', '{method}', '', '', '')"
            )
        return Diagnosis(
            exc.code,
            "依赖 mock 不够保真,被测服务因此失败",
            f"被测服务调用了 mock 未实现的方法:\n{listed}{snippet}",
            "在 dbus_testing/mocktemplates/<服务>.py 补上这些方法后重跑;"
            "补好的模板请回贡到框架仓,所有依赖同一服务的仓都会受益",
        )

    if isinstance(exc, ContractDrift):
        deltas = _ctx(exc, "deltas", []) or []
        detail = "\n".join(f"  {d.render()}" for d in deltas)
        return Diagnosis(
            exc.code,
            f"接口契约漂移({len(deltas)} 处差异)",
            detail,
            "接口是有意变更 → 跑 `dbus-testing scan --emit` 更新 contract.xml 并在 review 中说明;"
            "否则这是一次接口回归",
        )

    if isinstance(exc, GuardDenied):
        return Diagnosis(
            exc.code,
            "attach 只读护栏拒绝执行",
            exc.message,
            "attach 模式默认只读。确需调用请加入 service.yaml 的 allow.methods;"
            "更安全的做法是改用 mode: isolate 在私有总线上测",
        )

    if isinstance(exc, SignalTimeout):
        got = _ctx(exc, "received", []) or []
        detail = f"等待信号: {_ctx(exc, 'signal')}\n已收到的信号: {got if got else '(无)'}"
        return Diagnosis(
            exc.code,
            "等待信号超时",
            detail,
            "检查信号名拼写与触发方法是否正确;确认布扣在触发动作之前;必要时调大 timeout",
        )

    if isinstance(exc, CallTimeout):
        return Diagnosis(
            exc.code,
            "DBus 调用超时",
            f"目标: {_ctx(exc, 'target')}\n超时: {_ctx(exc, 'timeout')}s",
            "服务可能阻塞在该方法;用 --keep-bus 保留环境后手工 busctl call 复现",
        )

    if isinstance(exc, ConfigError):
        return Diagnosis(
            exc.code,
            "配置错误",
            exc.message,
            "对照 docs/primitives.md 检查字段拼写;未知字段会被拒绝以避免静默失效",
        )

    if isinstance(exc, AssertionFailed):
        return Diagnosis(exc.code, "断言不成立", exc.message, "")

    if isinstance(exc, BusError):
        return Diagnosis(
            exc.code,
            "总线不可用",
            exc.message,
            "isolate 模式需要 dbus-daemon 可执行;attach 模式需要 DBUS_SESSION_BUS_ADDRESS",
        )

    if isinstance(exc, DbusTestingError):
        return Diagnosis(exc.code, exc.message)

    return Diagnosis("E_INTERNAL", "框架内部错误", f"{type(exc).__name__}: {exc}")
