"""配置数据模型与加载(service.yaml / tests.yaml).

规则:
- apiVersion 分派:仅接受 API_VERSIONS 内的版本,未知版本报 E_CONFIG_INVALID。
- 未知字段一律报错(拼写错误必须早失败),不静默忽略。
- 变量展开:${BUILD_DIR} ${REPO_ROOT} ${TESTS_DIR} ${ENV:NAME}
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from .errors import ConfigError

API_VERSIONS = frozenset({"v1"})

Mode = Literal["isolate", "attach"]
Kind = Literal["process", "dsm", "plugin-host", "go-loader"]
StepOp = Literal["call", "get-prop", "set-prop", "wait-signal", "check-contract", "mock-state"]

STEP_OPS: tuple[str, ...] = (
    "call",
    "get-prop",
    "set-prop",
    "wait-signal",
    "check-contract",
    "mock-state",
)

DEFAULT_CALL_TIMEOUT = 5.0
DEFAULT_SIGNAL_TIMEOUT = 5.0
DEFAULT_READY_TIMEOUT = 10.0
LINE_KEY = "__line__"
# 未提供 --build-dir 时,含 ${BUILD_DIR} 的路径会被标记为该前缀
UNSET_MARKER = "<unset:"


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<missing>"


MISSING = _Missing()
UNSET = _Missing()


# --------------------------------------------------------------------------- 工具


class _LineLoader(yaml.SafeLoader):
    """记录每个映射的行号,供报告定位 tests.yaml:行号。"""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, Any]:
        mapping = super().construct_mapping(node, deep=deep)
        mapping[LINE_KEY] = node.start_mark.line + 1
        return mapping


def _load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
    try:
        return yaml.load(text, Loader=_LineLoader)  # noqa: S506 - 自定义 SafeLoader 子类
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败 {path}: {exc}") from exc


_VAR_RE = re.compile(r"\$\{(BUILD_DIR|REPO_ROOT|TESTS_DIR|ENV:[A-Za-z_][A-Za-z0-9_]*)\}")


def expand_vars(value: str, variables: dict[str, str]) -> str:
    """展开 ${BUILD_DIR} 等变量;未定义的变量报错而非留空。"""

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.startswith("ENV:"):
            env_name = name[4:]
            got = os.environ.get(env_name)
            if got is None:
                raise ConfigError(f"环境变量 {env_name} 未设置,但配置里引用了 ${{{name}}}")
            return got
        got = variables.get(name)
        if got is None:
            if name == "BUILD_DIR":
                # BUILD_DIR 是可选的:未传 --build-dir 时把候选标记为不可用,
                # 由 launcher 在候选清单里显示"已跳过",而不是让整份配置加载失败。
                return UNSET_MARKER + name + ">"
            raise ConfigError(f"变量 ${{{name}}} 未提供(通常需要在仓库内运行)")
        return got

    return _VAR_RE.sub(_sub, value)


def _expand_tree(node: Any, variables: dict[str, str]) -> Any:
    if isinstance(node, str):
        return expand_vars(node, variables)
    if isinstance(node, list):
        return [_expand_tree(x, variables) for x in node]
    if isinstance(node, dict):
        return {k: _expand_tree(v, variables) for k, v in node.items()}
    return node


def _as_dict(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} 必须是映射,实际是 {type(value).__name__}")
    return dict(value)


def _pop(d: dict[str, Any], key: str, default: Any = MISSING, *, where: str = "") -> Any:
    if key in d:
        return d.pop(key)
    if default is MISSING:
        raise ConfigError(f"{where or '配置'} 缺少必填字段: {key}")
    return default


def _pop_line(d: dict[str, Any]) -> int:
    line = d.pop(LINE_KEY, 0)
    return int(line) if isinstance(line, int) else 0


def _ensure_empty(d: dict[str, Any], where: str) -> None:
    d.pop(LINE_KEY, None)
    if d:
        keys = ", ".join(sorted(d))
        raise ConfigError(f"{where} 出现未知字段: {keys}")


def _as_str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(f"{where} 的元素必须是字符串,实际是 {type(item).__name__}")
            out.append(item)
        return tuple(out)
    raise ConfigError(f"{where} 必须是字符串或字符串列表")


_DURATION_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m)?\s*$")


def parse_duration(value: Any, where: str) -> float:
    """接受 10 / 10.5 / "10s" / "500ms" / "2m"。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m:
            num = float(m.group(1))
            unit = m.group(2) or "s"
            return num * {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]
    raise ConfigError(f"{where} 不是合法时长: {value!r}(示例: 5, 5s, 500ms)")


def service_name_to_path(name: str) -> str:
    """org.deepin.dde.Pinyin1 -> /org/deepin/dde/Pinyin1"""
    return "/" + name.replace(".", "/")


# --------------------------------------------------------------------------- service.yaml


@dataclass(frozen=True)
class SandboxSpec:
    home: Literal["tmp", "inherit"] = "tmp"
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MockSpec:
    template: str
    bus: Literal["session", "system"] = "session"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadySpec:
    name_owner: tuple[str, ...] = ()
    timeout: float = DEFAULT_READY_TIMEOUT


@dataclass(frozen=True)
class PolkitRule:
    interface: str
    methods: tuple[str, ...]


@dataclass(frozen=True)
class AuthSpec:
    polkit: tuple[PolkitRule, ...] = ()


@dataclass(frozen=True)
class IgnoreSpec:
    paths: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ("org.freedesktop.DBus.*",)
    methods: tuple[str, ...] = ()


DEFAULT_SRC_XML_GLOBS = ("api/dbus/*.xml", "xml/*.xml", "dbus/*.xml")


@dataclass(frozen=True)
class ServiceSpec:
    api_version: str
    process: str
    services: tuple[str, ...]
    mode: Mode = "isolate"
    kind: Kind = "process"
    binary_search: tuple[str, ...] = ()
    plugin: str | None = None
    module: str | None = None
    host_binary: str | None = None
    args: tuple[str, ...] = ()
    sandbox: SandboxSpec = field(default_factory=SandboxSpec)
    needs: tuple[MockSpec, ...] = ()
    system_bus: bool = False
    ready: ReadySpec = field(default_factory=ReadySpec)
    auth: AuthSpec = field(default_factory=AuthSpec)
    allow_methods: tuple[str, ...] = ()
    ignore: IgnoreSpec = field(default_factory=IgnoreSpec)
    src_xml_globs: tuple[str, ...] = DEFAULT_SRC_XML_GLOBS
    teardown: Literal["kill", "term"] = "kill"
    isolation: Literal["per-run", "per-case"] = "per-run"
    restart: Literal["never", "per-case"] = "never"
    tier: str | None = None
    source: Path = field(default=Path("."))
    repo_root: Path = field(default=Path("."))
    build_dir: Path | None = None

    @property
    def primary_service(self) -> str:
        return self.services[0]

    @property
    def default_path(self) -> str:
        return service_name_to_path(self.primary_service)

    def is_polkit_gated(self, interface: str, member: str) -> bool:
        """该方法是否在 auth.polkit 里被声明为受 polkit 门禁。"""
        for rule in self.auth.polkit:
            if rule.interface != interface:
                continue
            if not rule.methods or member in rule.methods:
                return True
        return False

    @property
    def has_polkit_rules(self) -> bool:
        return bool(self.auth.polkit)


def _one_of(value: Any, allowed: tuple[str, ...], where: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(f"{where} 必须是 {'/'.join(allowed)} 之一,实际是 {value!r}")
    return value


def _parse_sandbox(raw: Any) -> SandboxSpec:
    d = _as_dict(raw, "sandbox")
    home = _one_of(_pop(d, "home", None), ("tmp", "inherit"), "sandbox.home", "tmp")
    env_raw = _as_dict(_pop(d, "env", None), "sandbox.env")
    env_raw.pop(LINE_KEY, None)
    env = {str(k): str(v) for k, v in env_raw.items()}
    _ensure_empty(d, "sandbox")
    return SandboxSpec(home=home, env=env)  # type: ignore[arg-type]


def _parse_needs(raw: Any) -> tuple[MockSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("needs 必须是列表")
    out: list[MockSpec] = []
    for item in raw:
        if isinstance(item, str):
            out.append(MockSpec(template=item))
            continue
        d = _as_dict(item, "needs[]")
        template = _pop(d, "mock", MISSING, where="needs[]")
        if not isinstance(template, str):
            raise ConfigError("needs[].mock 必须是字符串")
        bus = _one_of(_pop(d, "bus", None), ("session", "system"), "needs[].bus", "session")
        params = _as_dict(_pop(d, "params", None), "needs[].params")
        params.pop(LINE_KEY, None)
        _ensure_empty(d, "needs[]")
        out.append(MockSpec(template=template, bus=bus, params=params))  # type: ignore[arg-type]
    return tuple(out)


def _parse_ready(raw: Any, services: tuple[str, ...]) -> ReadySpec:
    d = _as_dict(raw, "ready")
    names = _as_str_tuple(_pop(d, "name-owner", None), "ready.name-owner") or services
    timeout = parse_duration(_pop(d, "timeout", DEFAULT_READY_TIMEOUT), "ready.timeout")
    _ensure_empty(d, "ready")
    return ReadySpec(name_owner=names, timeout=timeout)


def _parse_auth(raw: Any) -> AuthSpec:
    d = _as_dict(raw, "auth")
    polkit_raw = _pop(d, "polkit", None)
    _ensure_empty(d, "auth")
    if polkit_raw is None:
        return AuthSpec()
    if not isinstance(polkit_raw, list):
        raise ConfigError("auth.polkit 必须是列表")
    rules: list[PolkitRule] = []
    for item in polkit_raw:
        item_d = _as_dict(item, "auth.polkit[]")
        iface = _pop(item_d, "interface", MISSING, where="auth.polkit[]")
        methods = _as_str_tuple(_pop(item_d, "methods", ()), "auth.polkit[].methods")
        _ensure_empty(item_d, "auth.polkit[]")
        rules.append(PolkitRule(interface=str(iface), methods=methods))
    return AuthSpec(polkit=tuple(rules))


def _parse_ignore(raw: Any) -> IgnoreSpec:
    d = _as_dict(raw, "ignore")
    paths = _as_str_tuple(_pop(d, "paths", ()), "ignore.paths")
    interfaces = _as_str_tuple(
        _pop(d, "interfaces", ("org.freedesktop.DBus.*",)), "ignore.interfaces"
    )
    methods = _as_str_tuple(_pop(d, "methods", ()), "ignore.methods")
    _ensure_empty(d, "ignore")
    return IgnoreSpec(paths=paths, interfaces=interfaces, methods=methods)


def load_service(
    path: Path,
    *,
    build_dir: Path | None = None,
    repo_root: Path | None = None,
) -> ServiceSpec:
    """加载并校验 service.yaml。"""
    path = Path(path)
    tests_dir = path.parent
    root = Path(repo_root) if repo_root else _guess_repo_root(tests_dir)
    variables: dict[str, str] = {
        "REPO_ROOT": str(root),
        "TESTS_DIR": str(tests_dir),
    }
    if build_dir is not None:
        variables["BUILD_DIR"] = str(Path(build_dir).resolve())

    raw = _load_yaml(path)
    d = _as_dict(raw, str(path))
    d = _expand_tree(d, variables)

    api_version = _pop(d, "apiVersion", MISSING, where=str(path))
    if api_version not in API_VERSIONS:
        raise ConfigError(
            f"{path}: 不支持的 apiVersion={api_version!r};本框架支持 {sorted(API_VERSIONS)}"
        )

    services = _as_str_tuple(_pop(d, "services", MISSING, where=str(path)), "services")
    if not services:
        raise ConfigError(f"{path}: services 至少需要一个服务名")
    process = _pop(d, "process", services[0])

    mode = _one_of(_pop(d, "mode", None), ("isolate", "attach"), "mode", "isolate")
    kind = _one_of(
        _pop(d, "kind", None), ("process", "dsm", "plugin-host", "go-loader"), "kind", "process"
    )

    binary_raw = _as_dict(_pop(d, "binary", None), "binary")
    binary_search = _as_str_tuple(_pop(binary_raw, "search", ()), "binary.search")
    _ensure_empty(binary_raw, "binary")

    spec = ServiceSpec(
        api_version=str(api_version),
        process=str(process),
        services=services,
        mode=mode,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        binary_search=binary_search,
        plugin=_opt_str(_pop(d, "plugin", None), "plugin"),
        module=_opt_str(_pop(d, "module", None), "module"),
        host_binary=_opt_str(_pop(d, "host-binary", None), "host-binary"),
        args=_as_str_tuple(_pop(d, "args", ()), "args"),
        sandbox=_parse_sandbox(_pop(d, "sandbox", None)),
        needs=_parse_needs(_pop(d, "needs", None)),
        system_bus=bool(_pop(d, "system-bus", False)),
        ready=_parse_ready(_pop(d, "ready", None), services),
        auth=_parse_auth(_pop(d, "auth", None)),
        allow_methods=_parse_allow(_pop(d, "allow", None)),
        ignore=_parse_ignore(_pop(d, "ignore", None)),
        src_xml_globs=_as_str_tuple(
            _pop(d, "src-xml-globs", DEFAULT_SRC_XML_GLOBS), "src-xml-globs"
        ),
        teardown=_one_of(  # type: ignore[arg-type]
            _pop(d, "teardown", None), ("kill", "term"), "teardown", "kill"
        ),
        isolation=_one_of(
            _pop(d, "isolation", None), ("per-run", "per-case"), "isolation", "per-run"
        ),  # type: ignore[arg-type]
        restart=_one_of(  # type: ignore[arg-type]
            _pop(d, "restart", None), ("never", "per-case"), "restart", "never"
        ),
        tier=_opt_str(_pop(d, "tier", None), "tier"),
        source=path,
        repo_root=root,
        build_dir=Path(build_dir).resolve() if build_dir is not None else None,
    )
    _ensure_empty(d, str(path))
    _validate_service(spec)
    return spec


def _opt_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where} 必须是字符串")
    return value


def _parse_allow(raw: Any) -> tuple[str, ...]:
    d = _as_dict(raw, "allow")
    methods = _as_str_tuple(_pop(d, "methods", ()), "allow.methods")
    _ensure_empty(d, "allow")
    return methods


def _validate_service(spec: ServiceSpec) -> None:
    if spec.mode == "isolate":
        for field_name, value in (("plugin", spec.plugin), ("host-binary", spec.host_binary)):
            if value and UNSET_MARKER in value:
                raise ConfigError(
                    f"{spec.source}: {field_name} 引用了 ${{BUILD_DIR}} 但未提供 --build-dir"
                )
        if spec.binary_search and all(UNSET_MARKER in c for c in spec.binary_search):
            raise ConfigError(
                f"{spec.source}: binary.search 的候选全部依赖 ${{BUILD_DIR}},"
                f"请传 --build-dir 指向构建目录"
            )
        if spec.kind in ("process", "go-loader") and not spec.binary_search:
            raise ConfigError(
                f"{spec.source}: kind={spec.kind} 且 mode=isolate 时必须提供 binary.search"
            )
        if spec.kind == "go-loader" and not spec.module:
            raise ConfigError(f"{spec.source}: kind=go-loader 必须提供 module(要启用的模块名)")
        if spec.kind in ("dsm", "plugin-host") and not spec.plugin:
            raise ConfigError(f"{spec.source}: kind={spec.kind} 必须提供 plugin(.so 路径)")
    for name in spec.ready.name_owner:
        if not name or name.startswith(":"):
            raise ConfigError(f"{spec.source}: ready.name-owner 含非法总线名 {name!r}")


def _guess_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return cur


# --------------------------------------------------------------------------- tests.yaml


@dataclass(frozen=True)
class Target:
    service: str
    path: str
    interface: str
    member: str

    @property
    def full_member(self) -> str:
        return f"{self.interface}.{self.member}"


@dataclass(frozen=True)
class Expect:
    signature: str | None = None
    value: Any = UNSET
    type: str | None = None
    error: Any = UNSET  # None=必须无错 / "*"=任意错 / "名"=指定错 / UNSET=隐含无错
    nonempty: bool | None = None

    @property
    def expects_error(self) -> bool:
        return self.error is not UNSET and self.error is not None


@dataclass(frozen=True)
class Step:
    op: StepOp
    target: Target | None = None
    args: tuple[Any, ...] = ()
    value: Any = None
    timeout: float | None = None
    expect: Expect = field(default_factory=Expect)
    signal: str | None = None
    mock_template: str | None = None
    mock_method: str | None = None
    line: int = 0


@dataclass(frozen=True)
class Case:
    name: str
    steps: tuple[Step, ...]
    flaky: int = 0
    line: int = 0
    skip: str | None = None


def _parse_expect(raw: Any) -> Expect:
    d = _as_dict(raw, "expect")
    signature = _opt_str(_pop(d, "signature", None), "expect.signature")
    type_ = _opt_str(_pop(d, "type", None), "expect.type")
    value = _pop(d, "value", UNSET)
    error = _pop(d, "error", UNSET)
    nonempty_raw = _pop(d, "nonempty", None)
    nonempty = None if nonempty_raw is None else bool(nonempty_raw)
    _ensure_empty(d, "expect")
    if error is not UNSET and error is not None and not isinstance(error, str):
        raise ConfigError("expect.error 必须是 null、\"*\" 或 DBus 错误名字符串")
    return Expect(
        signature=signature, value=value, type=type_, error=error, nonempty=nonempty
    )


def _parse_target(raw: dict[str, Any], spec: ServiceSpec, *, member_key: str) -> Target:
    service = _opt_str(_pop(raw, "service", None), "service") or spec.primary_service
    path = _opt_str(_pop(raw, "object", None), "object") or _opt_str(
        _pop(raw, "path", None), "path"
    )
    interface = _opt_str(_pop(raw, "interface", None), "interface") or service
    member = _pop(raw, member_key, MISSING, where=f"步骤({member_key})")
    if not isinstance(member, str):
        raise ConfigError(f"{member_key} 必须是字符串")
    if path is None:
        path = (
            spec.default_path
            if service == spec.primary_service
            else service_name_to_path(service)
        )
    return Target(service=service, path=path, interface=interface, member=member)


def _args_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _parse_step(entry: dict[str, Any], spec: ServiceSpec) -> Step:
    line = _pop_line(entry)
    present = [op for op in STEP_OPS if op in entry]
    if not present:
        raise ConfigError(
            f"tests.yaml:{line} 步骤缺少原语,必须含 {'/'.join(STEP_OPS)} 之一"
        )
    if len(present) > 1:
        raise ConfigError(
            f"tests.yaml:{line} 一个步骤只能含一个原语,实际含 {', '.join(present)};"
            f"多步请用 steps: 列表"
        )
    op = present[0]
    expect = _parse_expect(_pop(entry, "expect", None))
    timeout_raw = _pop(entry, "timeout", None)
    body_raw = entry.get(op)
    if timeout_raw is None and isinstance(body_raw, dict) and "timeout" in body_raw:
        # timeout 写在原语内部是最自然的位置,一并接受
        timeout_raw = body_raw.pop("timeout")
    timeout = None if timeout_raw is None else parse_duration(timeout_raw, "timeout")

    if op == "call":
        body = _as_dict(_pop(entry, "call"), "call")
        args = _args_tuple(_pop(body, "args", ()))
        target = _parse_target(body, spec, member_key="method")
        _ensure_empty(body, "call")
        _ensure_empty(entry, f"tests.yaml:{line} 步骤")
        return Step(op="call", target=target, args=args, expect=expect, timeout=timeout, line=line)

    if op in ("get-prop", "set-prop"):
        body = _as_dict(_pop(entry, op), op)
        value = _pop(body, "value", None)
        target = _parse_target(body, spec, member_key="property")
        _ensure_empty(body, op)
        _ensure_empty(entry, f"tests.yaml:{line} 步骤")
        return Step(
            op=op,  # type: ignore[arg-type]
            target=target,
            value=value,
            expect=expect,
            timeout=timeout,
            line=line,
        )

    if op == "wait-signal":
        body = _as_dict(_pop(entry, "wait-signal"), "wait-signal")
        name = _pop(body, "name", MISSING, where="wait-signal")
        sig_timeout_raw = _pop(body, "timeout", None)
        service = _opt_str(_pop(body, "service", None), "service") or spec.primary_service
        path = _opt_str(_pop(body, "object", None), "object") or _opt_str(
            _pop(body, "path", None), "path"
        )
        interface = _opt_str(_pop(body, "interface", None), "interface") or service
        _ensure_empty(body, "wait-signal")
        _ensure_empty(entry, f"tests.yaml:{line} 步骤")
        if path is None:
            path = (
                spec.default_path
                if service == spec.primary_service
                else service_name_to_path(service)
            )
        target = Target(service=service, path=path, interface=interface, member=str(name))
        eff_timeout = (
            parse_duration(sig_timeout_raw, "wait-signal.timeout")
            if sig_timeout_raw is not None
            else (timeout if timeout is not None else DEFAULT_SIGNAL_TIMEOUT)
        )
        return Step(
            op="wait-signal",
            target=target,
            signal=str(name),
            timeout=eff_timeout,
            expect=expect,
            line=line,
        )

    if op == "check-contract":
        _pop(entry, "check-contract")
        _ensure_empty(entry, f"tests.yaml:{line} 步骤")
        return Step(op="check-contract", expect=expect, line=line)

    # mock-state
    body = _as_dict(_pop(entry, "mock-state"), "mock-state")
    template = _pop(body, "template", MISSING, where="mock-state")
    method = _pop(body, "method", MISSING, where="mock-state")
    args = _args_tuple(_pop(body, "args", ()))
    _ensure_empty(body, "mock-state")
    _ensure_empty(entry, f"tests.yaml:{line} 步骤")
    return Step(
        op="mock-state",
        args=args,
        mock_template=str(template),
        mock_method=str(method),
        expect=expect,
        timeout=timeout,
        line=line,
    )


def _parse_case(entry: Any, spec: ServiceSpec, index: int) -> Case:
    d = _as_dict(entry, f"cases[{index}]")
    line = d.get(LINE_KEY, 0)
    name = _pop(d, "name", MISSING, where=f"cases[{index}]")
    flaky = int(_pop(d, "flaky", 0) or 0)
    skip = _opt_str(_pop(d, "skip", None), "skip")
    steps_raw = _pop(d, "steps", None)
    if steps_raw is not None:
        if not isinstance(steps_raw, list):
            raise ConfigError(f"cases[{index}].steps 必须是列表")
        _pop_line(d)
        _ensure_empty(d, f"cases[{index}]")
        steps = tuple(_parse_step(_as_dict(s, "steps[]"), spec) for s in steps_raw)
    else:
        # 单条目形式:允许 call + wait-signal 组合(触发后等信号)
        combo = [op for op in STEP_OPS if op in d]
        if len(combo) > 1:
            expect = _pop(d, "expect", None)
            sub_steps: list[Step] = []
            # 先编译非 wait-signal 原语,expect 归属于它;wait-signal 作为后续步骤
            for op in combo:
                piece: dict[str, Any] = {op: d.pop(op), LINE_KEY: line}
                if op != "wait-signal" and expect is not None:
                    piece["expect"] = expect
                sub_steps.append(_parse_step(piece, spec))
            _pop_line(d)
            _ensure_empty(d, f"cases[{index}]")
            sub_steps.sort(key=lambda s: s.op == "wait-signal")
            steps = tuple(sub_steps)
        else:
            steps = (_parse_step(d, spec),)
    if not isinstance(name, str):
        raise ConfigError(f"cases[{index}].name 必须是字符串")
    return Case(name=name, steps=steps, flaky=flaky, line=int(line), skip=skip)


def load_cases(path: Path, spec: ServiceSpec) -> list[Case]:
    """加载 tests.yaml;文件不存在时返回空列表(只跑契约校验)。"""
    path = Path(path)
    if not path.exists():
        return []
    raw = _load_yaml(path)
    d = _as_dict(raw, str(path))
    api_version = _pop(d, "apiVersion", "v1")
    if api_version not in API_VERSIONS:
        raise ConfigError(f"{path}: 不支持的 apiVersion={api_version!r}")
    cases_raw = _pop(d, "cases", [])
    _ensure_empty(d, str(path))
    if cases_raw is None:
        return []
    if not isinstance(cases_raw, list):
        raise ConfigError(f"{path}: cases 必须是列表")
    cases = [_parse_case(entry, spec, i) for i, entry in enumerate(cases_raw)]
    names = [c.name for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"{path}: 用例名重复: {', '.join(sorted(dupes))}")
    return cases


def with_build_dir(spec: ServiceSpec, build_dir: Path | None) -> ServiceSpec:
    return replace(spec, build_dir=Path(build_dir).resolve() if build_dir else None)
