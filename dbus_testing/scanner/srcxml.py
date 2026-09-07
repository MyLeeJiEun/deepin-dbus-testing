"""仓内源码声明 XML 的收集与静态比对(**只读**,绝不写入被测仓)。

多数 DDE 仓维护着与 introspection 同格式的接口声明(`api/dbus/*.xml` 等),
`check --static` 拿它与 contract.xml 直比:不起服务、秒级,密闭跑不起来的服务也守得住。

源码 XML 通常不含对象路径,统一归入 <object path="*">;静态比对只看接口与成员,
不比路径。仓内没有任何源码 XML 时 src_baseline_xml 返回 None(上层输出 SKIP,不是失败)。
"""

from __future__ import annotations

from glob import glob
from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import ConfigError
from ..model import IgnoreSpec
from ..results import Delta
from .diff import DeltaKind, diff_members
from .normalize import MemberTable, local_name, member_index, normalize_interface_xml

#: 源码声明无路径信息,统一挂在这个占位路径下
SRC_PATH = "*"


def _interfaces_in(path: Path) -> list[ET.Element]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"源码 XML 读取失败 {path}: {exc}") from exc
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        # 文件是用户用 src-xml-globs 显式指到的,坏文件必须报出来,不能静默从静态门禁里消失
        raise ConfigError(f"源码 XML 解析失败 {path}: {exc}") from exc
    if local_name(root.tag) == "interface":
        return [root]
    return [elem for elem in root.iter() if local_name(elem.tag) == "interface"]


def collect_src_xml(repo_root: Path, globs: tuple[str, ...]) -> dict[str, str]:
    """按 globs 收集源码声明,建 {接口名: 该接口的 XML 片段} 索引。

    同一接口在多处声明时保留首个命中(glob 次序 + 路径字典序),保证结果稳定可复现。
    非 introspection 格式的 XML 自然贡献 0 个接口,不报错。
    """
    found: dict[str, str] = {}
    for pattern in globs:
        for rel in sorted(glob(pattern, root_dir=repo_root, recursive=True)):
            path = repo_root / rel  # 绝对模式时 rel 已是绝对路径,/ 运算取右侧
            if not path.is_file():
                continue
            for iface in _interfaces_in(path):
                name = iface.get("name", "")
                if name and name not in found:
                    found[name] = ET.tostring(iface, encoding="unicode")
    return found


def src_baseline_xml(repo_root: Path, globs: tuple[str, ...], ignore: IgnoreSpec) -> str | None:
    """把源码声明汇总为与 normalize_tree 同格式的单份规范化 XML;无源码 XML 时返回 None。"""
    fragments = collect_src_xml(repo_root, globs)
    if not fragments:
        return None
    body = "".join(fragments[name] for name in sorted(fragments))
    doc = f'<node><object path="{SRC_PATH}">{body}</object></node>'
    return normalize_interface_xml(doc, ignore)


def _flatten(normalized_xml: str) -> dict[str, MemberTable]:
    """丢掉路径维度,合并为 {接口: {(kind, 成员): 签名}}。"""
    flat: dict[str, MemberTable] = {}
    for table in member_index(normalized_xml).values():
        for interface, members in table.items():
            flat.setdefault(interface, {}).update(members)
    return flat


def static_diff(src_xml: str, baseline_xml: str) -> list[Delta]:
    """源码声明 -> 基线的静态漂移(before=源码,after=基线),忽略对象路径差异。

    只有两边共有的接口才逐成员比对;源码有而基线没有 -> removed(基线该补),
    基线有而源码没有 -> added(基线多出,可能是运行时动态注册或基线过期)。
    """
    src = _flatten(src_xml)
    baseline = _flatten(baseline_xml)
    deltas: list[Delta] = []
    for interface in sorted(set(src) | set(baseline)):
        if interface in src and interface in baseline:
            deltas.extend(diff_members(SRC_PATH, interface, src[interface], baseline[interface]))
            continue
        kind: DeltaKind = "removed" if interface in src else "added"
        deltas.append(Delta(kind=kind, scope="interface", path=SRC_PATH, interface=interface))
    return deltas
