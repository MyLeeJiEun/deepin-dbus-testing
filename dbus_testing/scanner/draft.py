"""init 脚手架:从运行时接口生成 service.yaml / tests.yaml 草稿.

价值主张:开发者的工作从"从零写配置"变成"删改草稿"。
- 无入参且有出参的方法 → 生成**可直接启用**的签名断言;
- 只读属性 → 生成**可直接启用**的类型断言 + 只读写入的错误路径;
- 有入参的方法 / 信号 → 生成注释态模板,提示补参数或触发方法。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..model import IgnoreSpec
from .normalize import member_index

# DBus 类型码 → YAML 占位值
_PLACEHOLDER: dict[str, str] = {
    "s": '""',
    "o": '"/"',
    "g": '""',
    "b": "false",
    "y": "0",
    "n": "0",
    "q": "0",
    "i": "0",
    "u": "0",
    "x": "0",
    "t": "0",
    "d": "0.0",
    "h": "0",
    "v": '""',
}


@dataclass
class ProbeResult:
    """init 探测到的事实,用于生成 service.yaml。"""

    services: tuple[str, ...]
    binary: Path | None = None
    binary_search: tuple[str, ...] = ()
    kind: str = "process"
    mode: str = "isolate"
    env: dict[str, str] = field(default_factory=dict)
    needs: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)
    module: str | None = None
    plugin: str | None = None
    args: tuple[str, ...] = ()


def _split_args(sig: str) -> list[str]:
    """把参数签名串拆成单个完整类型(处理 a/(...)/{...} 嵌套)。"""
    out: list[str] = []
    i = 0
    while i < len(sig):
        start = i
        while i < len(sig) and sig[i] == "a":
            i += 1
        if i < len(sig) and sig[i] == "(":
            depth = 1
            i += 1
            while i < len(sig) and depth:
                if sig[i] == "(":
                    depth += 1
                elif sig[i] == ")":
                    depth -= 1
                i += 1
        elif i < len(sig) and sig[i] == "{":
            depth = 1
            i += 1
            while i < len(sig) and depth:
                if sig[i] == "{":
                    depth += 1
                elif sig[i] == "}":
                    depth -= 1
                i += 1
        else:
            i += 1
        out.append(sig[start:i])
    return out


def _placeholder(type_code: str) -> str:
    if type_code.startswith("a{"):
        return "{}"
    if type_code.startswith("a"):
        return "[]"
    if type_code.startswith("("):
        return "[]"
    return _PLACEHOLDER.get(type_code, '""')


def _parse_method_sig(sig: str) -> tuple[str, str]:
    """method 的 signature 形如 'in=ss,out=s'。"""
    in_sig, out_sig = "", ""
    for part in sig.split(","):
        key, _, val = part.partition("=")
        if key == "in":
            in_sig = val
        elif key == "out":
            out_sig = val
    return in_sig, out_sig


def _parse_prop_sig(sig: str) -> tuple[str, str]:
    """property 的 signature 形如 'type=s,access=read'。"""
    type_code, access = "", "read"
    for part in sig.split(","):
        key, _, val = part.partition("=")
        if key == "type":
            type_code = val
        elif key == "access":
            access = val
    return type_code, access


# Go 的 flag 包对 `-enable` 与 `--enable` 一视同仁,两种写法都要认
_ENABLE_FLAGS = ("--enable", "-enable")
# launcher 组 go-loader argv 时会自己补 -i(忽略缺失模块),这里剥掉避免重复
_REDUNDANT_LOADER_FLAGS = frozenset({"-i", "--ignore", "-ignore"})


def split_loader_args(args: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    """从探测用的 argv 里识别 `--enable <module>`,转成 go-loader 的规范形态。

    返回 (module, 其余参数)。识别不到就返回 (None, 原样参数)。
    同时剥掉 launcher 会自行补上的 -i/--ignore,避免生成 `-i -i` 这种重复 argv。
    """
    rest: list[str] = []
    module: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _ENABLE_FLAGS and i + 1 < len(args):
            module = args[i + 1]
            i += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in _ENABLE_FLAGS):
            module = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    if module is not None:
        rest = [a for a in rest if a not in _REDUNDANT_LOADER_FLAGS]
    return module, tuple(rest)


def draft_service(probe: ProbeResult) -> str:
    """生成 service.yaml 文本。"""
    lines: list[str] = [
        "# 由 `dbus-testing init` 生成。请审阅后提交。",
        "apiVersion: v1",
    ]
    primary = probe.services[0] if probe.services else "org.example.Unknown"
    kind = probe.kind
    module = probe.module
    args = probe.args
    if module is None:
        detected, args = split_loader_args(args)
        if detected is not None:
            module = detected
            kind = "go-loader"
            probe.notes.append(
                f"探测参数里含 --enable {detected},已按 loader 模块形态生成 "
                f"kind: go-loader + module(argv 由框架组装为 "
                f"`<binary> --enable {detected} -i`)"
            )
    lines.append(f"process: {probe.binary.name if probe.binary else primary}")
    lines.append(f"mode: {probe.mode}")
    lines.append(f"kind: {kind}")
    lines.append("")
    lines.append("services:")
    for svc in probe.services:
        lines.append(f"  - {svc}")
    lines.append("")

    if probe.binary_search:
        lines.append("binary:")
        lines.append("  search:")
        for cand in probe.binary_search:
            lines.append(f"    - {cand}")
        lines.append("")
    if module:
        lines.append(f"module: {module}")
    if probe.plugin:
        lines.append(f"plugin: {probe.plugin}")
    if args:
        rendered = ", ".join(f'"{a}"' for a in args)
        lines.append(f"args: [{rendered}]")
    if module or args:
        lines.append("")

    lines.append("sandbox:")
    lines.append("  home: tmp")
    if probe.env:
        lines.append("  env:")
        for key, value in sorted(probe.env.items()):
            lines.append(f"    {key}: {value}")
    lines.append("")

    if probe.needs:
        lines.append("needs:")
        for dep in probe.needs:
            lines.append(f"  - mock: {dep}")
    else:
        lines.append("# 若服务依赖外部 DBus 服务,在这里前置 mock(必须先于被测进程启动):")
        lines.append("# needs:")
        lines.append("#   - mock: systemd")
        lines.append("#   - mock: org.desktopspec.ConfigManager")
    lines.append("")

    lines.append("ready:")
    lines.append("  name-owner:")
    for svc in probe.services:
        lines.append(f"    - {svc}")
    lines.append("  timeout: 10s")
    lines.append("")
    lines.append("# attach 模式安全默认:只读。需要调用方法时在这里显式放行。")
    lines.append("allow:")
    lines.append("  methods: []")
    lines.append("")
    lines.append("ignore:")
    lines.append("  interfaces:")
    lines.append('    - "org.freedesktop.DBus.*"')
    lines.append("")
    lines.append("teardown: kill")

    if probe.notes:
        lines.append("")
        lines.append("# init 探测记录:")
        for note in probe.notes:
            lines.append(f"#   - {note}")
    return "\n".join(lines) + "\n"


def draft_tests(
    normalized_xml: str,
    ignore: IgnoreSpec,
    *,
    primary_service: str,
    primary_path: str,
) -> tuple[str, int, int]:
    """生成 tests.yaml 草稿。

    返回 (文本, 可直接启用的用例数, 成员总数)。

    实现要点:必须用 **path 感知** 的 member_index —— 一个进程常注册多个服务名/多个
    对象路径(如 dde-session-daemon 同时提供 Daemon1 / Format1 / Timedate1),
    只写 interface 而不写 object 会把请求发到默认路径上,得到 InterfaceNotFound。
    """
    index = member_index(normalized_xml)
    header = [
        "# 由 `dbus-testing init` 生成的用例草稿。",
        "# 未注释的用例可直接运行;注释态用例请补齐参数/触发方法后取消注释。",
        "apiVersion: v1",
        "cases:",
    ]
    body: list[str] = []
    ready = 0
    total = 0
    used_names: set[str] = set()

    def uniq(name: str) -> str:
        if name not in used_names:
            used_names.add(name)
            return name
        for i in range(2, 100):
            candidate = f"{name}-{i}"
            if candidate not in used_names:
                used_names.add(candidate)
                return candidate
        return name  # pragma: no cover - 不可能有 100 个同名

    for path in sorted(index):
        if "*" in path:
            # 归并后的动态子对象是模式,不是真实路径,不能拿来发调用
            continue
        for interface in sorted(index[path]):
            entries = index[path][interface]
            if not entries:
                continue
            body.append(f"  # ---- {interface} @ {path} ----")
            loc = _target_prefix(
                path=path,
                interface=interface,
                primary_path=primary_path,
                primary_service=primary_service,
            )
            for (kind, member), sig in sorted(entries.items(), key=lambda kv: kv[0][1]):
                total += 1
                slug = _slug(member)
                if kind == "method":
                    in_sig, out_sig = _parse_method_sig(sig)
                    if not in_sig and out_sig:
                        name = uniq(f"{slug}-signature")
                        body.append(f"  - name: {name}")
                        body.append(f"    call: {{{loc}method: {member}}}")
                        body.append(f'    expect: {{signature: "{out_sig}"}}')
                        ready += 1
                    else:
                        name = uniq(f"{slug}-smoke")
                        args = ", ".join(_placeholder(t) for t in _split_args(in_sig))
                        body.append(f"  # - name: {name}   # 入参签名: {in_sig or '(无)'}")
                        body.append(f"  #   call: {{{loc}method: {member}, args: [{args}]}}")
                        if out_sig:
                            body.append(f'  #   expect: {{signature: "{out_sig}"}}')
                        else:
                            body.append("  #   expect: {error: null}")
                        body.append(
                            '  #   # 补真实参数后取消注释;或改为错误路径 expect: {error: "*"}'
                        )
                elif kind == "property":
                    type_code, access = _parse_prop_sig(sig)
                    name = uniq(f"{slug}-readable")
                    body.append(f"  - name: {name}")
                    body.append(f"    get-prop: {{{loc}property: {member}}}")
                    body.append(f'    expect: {{type: "{type_code}"}}')
                    ready += 1
                    if access == "read":
                        name = uniq(f"{slug}-readonly")
                        body.append(f"  - name: {name}")
                        body.append(
                            f"    set-prop: {{{loc}property: {member}, "
                            f"value: {_placeholder(type_code)}}}"
                        )
                        # 只读属性的错误名各实现不同(dbus-python 给 PropertyReadOnly,
                        # Go dbusutil 给 Failed),草稿先用宽断言,确认后请收紧成具体错误名
                        body.append('    expect: {error: "*"}   # 建议收紧为实际错误名')
                        ready += 1
                    else:
                        name = uniq(f"{slug}-roundtrip")
                        body.append(f"  # - name: {name}   # 可写属性:读-写-读回")
                        body.append("  #   steps:")
                        body.append(f"  #     - get-prop: {{{loc}property: {member}}}")
                        body.append(
                            f"  #     - set-prop: {{{loc}property: {member}, "
                            f"value: {_placeholder(type_code)}}}"
                        )
                        body.append("  #       expect: {error: null}")
                else:  # signal
                    name = uniq(f"{slug}-emitted")
                    body.append(f"  # - name: {name}   # 信号载荷签名: {sig or '(无)'}")
                    body.append("  #   steps:")
                    body.append("  #     - call: {method: 触发该信号的方法}")
                    body.append(f"  #     - wait-signal: {{{loc}name: {member}, timeout: 5s}}")
            body.append("")

    body.append("  # 契约漂移检查:运行时接口必须与 contract.xml 一致")
    body.append("  - name: contract")
    body.append("    check-contract: true")
    ready += 1
    return "\n".join([*header, *body]) + "\n", ready, total


def _target_prefix(
    *, path: str, interface: str, primary_path: str, primary_service: str
) -> str:
    """按需生成 `object: ..., interface: ..., ` 前缀;与默认值相同时省略。"""
    parts: list[str] = []
    if path != primary_path:
        parts.append(f"object: {path}")
    if interface != primary_service:
        parts.append(f"interface: {interface}")
    return ("".join(f"{p}, " for p in parts)) if parts else ""


_SLUG_RE1 = re.compile(r"(.)([A-Z][a-z]+)")
_SLUG_RE2 = re.compile(r"([a-z0-9])([A-Z])")


def _slug(name: str) -> str:
    """CamelCase → kebab-case,正确处理连续大写缩写。

    CanNTP → can-ntp;LocalRTC → local-rtc;NTPServer → ntp-server;
    Use24HourFormat → use24-hour-format
    """
    step1 = _SLUG_RE1.sub(r"\1-\2", name)
    return _SLUG_RE2.sub(r"\1-\2", step1).lower()
