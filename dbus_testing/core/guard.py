"""attach 模式只读护栏.

安全默认:接真实会话时默认只读(Introspect / Properties.Get / 契约校验),
任何方法调用与属性写入必须在 service.yaml 的 allow.methods 中显式放行。
理由:真实会话里误调 SessionManager1.Logout / Shutdown 会直接搞掉开发者或 CI 的会话。
"""

from __future__ import annotations

from fnmatch import fnmatch

from ..errors import GuardDenied
from ..model import ServiceSpec, Step

READ_ONLY_OPS = frozenset({"get-prop", "check-contract"})
MUTATING_OPS = frozenset({"call", "set-prop", "mock-state"})


class AttachGuard:
    def __init__(self, spec: ServiceSpec) -> None:
        self.spec = spec
        self.enabled = spec.mode == "attach"
        self.allow = tuple(spec.allow_methods)

    def check(self, step: Step) -> None:
        if not self.enabled:
            return
        if step.op in READ_ONLY_OPS or step.op == "wait-signal":
            return
        if step.op == "mock-state":
            raise GuardDenied(
                "attach 模式下不允许 mock-state:真实会话中的依赖不能被替换;"
                "请改用 mode: isolate"
            )
        target = step.target
        name = target.full_member if target else step.op
        if any(fnmatch(name, pattern) for pattern in self.allow):
            return
        raise GuardDenied(
            f"attach 模式默认只读,拒绝执行 {name}。"
            f"确需调用请在 service.yaml 的 allow.methods 中显式放行"
            f"(注意破坏性风险:真实会话中的注销/关机/写配置类方法会造成实际影响)",
            member=name,
            allow=list(self.allow),
        )
