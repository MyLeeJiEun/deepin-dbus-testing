"""introspection XML 规范化(契约基线的稳定性关键,严格按开发文档 §2.11 七步)。

七步:
1. ElementTree 解析(注释与处理指令由解析器天然丢弃);
2. 删除 <annotation> 与纯空白文本 —— 渲染只走 interface/method/signal/property/arg 白名单;
3. 丢弃 interface name 命中 ignore.interfaces(fnmatch,区分大小写)的接口;
4. 丢弃成员全名 "接口.成员" 命中 ignore.methods 的方法/属性/信号;
5. 排序:对象路径按字典序;interface 按 name;interface 内先 method、再 signal、再 property。
   ⚠ <arg> 保持声明顺序 —— 参数顺序是语义,不可排序;
6. 标签属性按 (name, type, access, direction) 固定顺序输出;
7. 2 空格缩进、UTF-8、行尾 LF。

输出根元素统一为 <node>,内部按对象路径分组为 <object path="...">;文档不携带路径信息时
interface 直接挂在 <node> 下。两种形态都能被再次解析,故幂等成立:
normalize(normalize(x)) == normalize(x)。

子对象引用 <node name="child"/> 不参与输出:整棵树由 normalize_tree 的路径键覆盖,
保留引用只会引入动态子对象带来的 diff 噪声。
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Literal
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from ..errors import ConfigError
from ..model import IgnoreSpec

MemberScope = Literal["method", "signal", "property"]
MemberKey = tuple[MemberScope, str]
#: (kind, 成员名) -> 签名串
MemberTable = dict[MemberKey, str]
#: 对象路径 -> 接口名 -> 成员表
MemberIndex = dict[str, dict[str, MemberTable]]

_MEMBER_SCOPES: dict[str, MemberScope] = {
    "method": "method",
    "signal": "signal",
    "property": "property",
}
# 步骤⑤ 的成员分组次序
_SCOPE_ORDER: dict[str, int] = {"method": 0, "signal": 1, "property": 2}
# 步骤⑥ 的固定属性次序
_ATTR_ORDER = ("name", "type", "access", "direction")
_INDENT = "  "

# 一个对象组:(对象路径, 是否为 ignore.paths 归并结果, interface 元素);路径 "" 表示无路径信息
_Group = tuple[str, bool, list[ET.Element]]


# --------------------------------------------------------------------------- 解析


def local_name(tag: str) -> str:
    """去掉可能存在的 {namespace} 前缀。"""
    return tag.rpartition("}")[2]


def _parse(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ConfigError(f"introspection XML 解析失败: {exc}") from exc


def _root_path(root: ET.Element) -> str:
    """<node name="/org/..."> 携带的绝对路径;相对名是子对象引用,不算路径信息。"""
    name = root.get("name", "")
    return name if name.startswith("/") else ""


def _collect_objects(xml_text: str) -> list[_Group]:
    """取出文档中的对象组。

    同时接受原始 introspection 文档(interface 直接挂在 <node> 下)与本模块输出的
    规范化文档(<object path="..."> 分组),后者是幂等的前提。
    """
    root = _parse(xml_text)
    if local_name(root.tag) == "interface":
        return [("", False, [root])]
    groups: list[_Group] = [
        (
            obj.get("path", ""),
            obj.get("collapsed") == "true",
            [child for child in obj if local_name(child.tag) == "interface"],
        )
        for obj in root
        if local_name(obj.tag) == "object"
    ]
    direct = [child for child in root if local_name(child.tag) == "interface"]
    if direct:
        groups.append((_root_path(root), False, direct))
    return groups


def _interfaces_of(xml_text: str) -> list[ET.Element]:
    return [iface for _path, _collapsed, ifaces in _collect_objects(xml_text) for iface in ifaces]


def _match(value: str, patterns: tuple[str, ...]) -> str | None:
    """返回首个命中的 fnmatch 模式;DBus 名区分大小写,故用 fnmatchcase。"""
    for pattern in patterns:
        if fnmatchcase(value, pattern):
            return pattern
    return None


def _prepare(groups: list[_Group], ignore: IgnoreSpec) -> list[_Group]:
    """步骤③ 接口过滤 + ignore.paths 归并;返回按路径字典序排列、非空的对象组。"""
    merged: dict[str, _Group] = {}
    for path, collapsed, ifaces in groups:
        pattern = _match(path, ignore.paths) if path else None
        key = pattern or path
        if pattern is not None and key in merged:
            continue  # 同一模式只保留第一个匹配路径的内容
        kept = [i for i in ifaces if _match(i.get("name", ""), ignore.interfaces) is None]
        if key in merged:
            _, was_collapsed, existing = merged[key]
            merged[key] = (key, was_collapsed or collapsed, existing + kept)
        else:
            merged[key] = (key, collapsed or pattern is not None, kept)
    # 过滤后无接口的对象不进基线:中间路径只有标准接口,留空节点纯属噪声
    return [merged[key] for key in sorted(merged) if merged[key][2]]


# --------------------------------------------------------------------------- 渲染


def _attr_value(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _attr_text(attrib: dict[str, str]) -> str:
    """步骤⑥:固定次序在前,其余属性按名排序(不静默丢弃未知属性)。"""
    ordered = [(key, attrib[key]) for key in _ATTR_ORDER if key in attrib]
    ordered += sorted((k, v) for k, v in attrib.items() if k not in _ATTR_ORDER)
    return "".join(f' {key}="{_attr_value(value)}"' for key, value in ordered)


def _arg_attrib(kind: str, attrib: dict[str, str]) -> dict[str, str]:
    """method 参数补齐 direction 默认值 in;signal 参数恒为 out,省略 direction。

    源码 XML 常省略 direction="in",运行时 introspect 一般显式给出;统一到同一形态才能
    让"源码声明 / 基线 / 运行时"三方逐字符可比。
    """
    out = dict(attrib)
    if kind == "signal":
        out.pop("direction", None)
    else:
        out.setdefault("direction", "in")
    return out


def _render_member(member: ET.Element, indent: str, lines: list[str]) -> None:
    kind = local_name(member.tag)
    args = [child for child in member if local_name(child.tag) == "arg"]
    head = f"{indent}<{kind}{_attr_text(member.attrib)}"
    if not args:
        lines.append(head + "/>")
        return
    lines.append(head + ">")
    for arg in args:  # 声明顺序即语义,不排序
        lines.append(f"{indent}{_INDENT}<arg{_attr_text(_arg_attrib(kind, arg.attrib))}/>")
    lines.append(f"{indent}</{kind}>")


def _render_interfaces(
    ifaces: list[ET.Element], indent: str, drop: tuple[str, ...], lines: list[str]
) -> None:
    for iface in sorted(ifaces, key=lambda e: e.get("name", "")):
        iface_name = iface.get("name", "")
        members = [
            child
            for child in iface
            if local_name(child.tag) in _SCOPE_ORDER
            and _match(f"{iface_name}.{child.get('name', '')}", drop) is None
        ]
        members.sort(key=lambda e: (_SCOPE_ORDER[local_name(e.tag)], e.get("name", "")))
        head = f"{indent}<interface{_attr_text(iface.attrib)}"
        if not members:
            lines.append(head + "/>")
            continue
        lines.append(head + ">")
        for member in members:
            _render_member(member, indent + _INDENT, lines)
        lines.append(f"{indent}</interface>")


def _render(groups: list[_Group], drop: tuple[str, ...]) -> str:
    lines = ["<node>"]
    for path, collapsed, ifaces in groups:
        if not path:
            _render_interfaces(ifaces, _INDENT, drop, lines)
            continue
        head = f'{_INDENT}<object path="{_attr_value(path)}"'
        if collapsed:
            head += ' collapsed="true"'
        lines.append(head + ">")
        _render_interfaces(ifaces, _INDENT * 2, drop, lines)
        lines.append(f"{_INDENT}</object>")
    lines.append("</node>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- 公开接口


def normalize_interface_xml(xml_text: str, ignore: IgnoreSpec) -> str:
    """规范化单份 introspection 文档;对自身输出幂等。"""
    return _render(_prepare(_collect_objects(xml_text), ignore), ignore.methods)


def normalize_tree(tree: dict[str, str], ignore: IgnoreSpec) -> str:
    """把 {对象路径: introspection XML} 汇总为单份规范化 XML。

    路径以字典键为准(walk 时已知),文档内的 <node name> 不参与;命中 ignore.paths 的
    路径归并为 <object path="命中的模式" collapsed="true">,同一模式只留第一个匹配路径的内容。
    """
    groups: list[_Group] = [(path, False, _interfaces_of(tree[path])) for path in sorted(tree)]
    return _render(_prepare(groups, ignore), ignore.methods)


def _signature(scope: MemberScope, member: ET.Element) -> str:
    """method -> "in=ss,out=s";property -> "type=s,access=read";signal -> "ai"。"""
    if scope == "property":
        return f"type={member.get('type', '')},access={member.get('access', '')}"
    args = [child for child in member if local_name(child.tag) == "arg"]
    types = [(arg.get("direction", "in"), arg.get("type", "")) for arg in args]
    if scope == "signal":
        return "".join(sig for _direction, sig in types)
    ins = "".join(sig for direction, sig in types if direction != "out")
    outs = "".join(sig for direction, sig in types if direction == "out")
    return f"in={ins},out={outs}"


def member_index(normalized_xml: str) -> MemberIndex:
    """建索引:对象路径 -> 接口 -> (kind, 成员) -> 签名。diff 与 static_diff 的共同底座。"""
    index: MemberIndex = {}
    for path, _collapsed, ifaces in _collect_objects(normalized_xml):
        table = index.setdefault(path, {})
        for iface in ifaces:
            members = table.setdefault(iface.get("name", ""), {})
            for child in iface:
                scope = _MEMBER_SCOPES.get(local_name(child.tag))
                if scope is None:
                    continue
                members[(scope, child.get("name", ""))] = _signature(scope, child)
    return index


def parse_members(normalized_xml: str) -> dict[str, dict[str, tuple[str, str]]]:
    """提取成员表 {接口: {成员: (kind, 签名)}},跨对象路径合并(覆盖矩阵按接口维度统计)。"""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for table in member_index(normalized_xml).values():
        for iface, members in table.items():
            slot = out.setdefault(iface, {})
            for (scope, name), sig in members.items():
                slot[name] = (scope, sig)
    return out
