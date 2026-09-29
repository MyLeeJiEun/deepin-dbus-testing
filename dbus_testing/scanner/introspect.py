"""运行时接口快照:遍历对象树并抓取 introspection XML。

与 ignore.paths 的配合(实现要点):动态子对象(ObjectManager 模式)常有成百上千个兄弟路径。
逐模式只保留**第一个**命中作为样本(walk 的 "prune":introspect 但不向下展开),
其余兄弟直接 "skip"。这样规范化阶段能把它们归并成一个 collapsed 节点,
基线里看得见这一节,又不会把整棵动态子树都 introspect 一遍。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from fnmatch import fnmatchcase

from ..core.client import BusClient
from ..model import IgnoreSpec
from .normalize import local_name

log = logging.getLogger(__name__)

ObjectTree = dict[str, str]


def make_decider(ignore: IgnoreSpec) -> object:
    """返回 walk 用的三态决策函数(逐模式计数,首个命中留样本)。"""
    seen: dict[str, int] = {}
    patterns = tuple(ignore.paths)

    def decide(path: str) -> str:
        for pat in patterns:
            if fnmatchcase(path, pat):
                count = seen.get(pat, 0)
                seen[pat] = count + 1
                return "prune" if count == 0 else "skip"
        return "keep"

    return decide


def snapshot(
    client: BusClient,
    services: tuple[str, ...],
    ignore: IgnoreSpec,
    *,
    root: str = "/",
) -> ObjectTree:
    """抓取给定服务名下的全部对象与其 introspection XML。

    同一进程的多个服务名可能共享路径且各导不同接口:introspect 按 destination
    寻址,同连接同路径的应答可以随名字不同。按路径去重会把第二个名字的接口
    静默丢进基线盲区,因此同路径的多份文档按接口取并集。
    """
    tree: ObjectTree = {}
    for service in services:
        decide = make_decider(ignore)
        for path, xml in client.walk(service, root, decide=decide):  # type: ignore[arg-type]
            if path in tree:
                tree[path] = merge_docs(tree[path], xml)
            else:
                tree[path] = xml
    return tree


def merge_docs(a: str, b: str) -> str:
    """合并同一路径的两份 introspection 文档:接口按名取并集。

    内容一致的同名接口只留一份;同名不同内容属服务端异常,保留首个并告警
    (与 srcxml 收集源码声明时的"同一接口保留首个命中"同一语义)。
    init 对双总线探测合并两棵快照树时也用它。
    """
    by_name: dict[str, ET.Element] = {}
    for doc in (a, b):
        try:
            root = ET.fromstring(doc)
        except ET.ParseError:
            continue
        for child in root:
            if local_name(child.tag) != "interface":
                continue
            name = child.get("name", "")
            kept = by_name.get(name)
            if kept is None:
                by_name[name] = child
            elif _iface_key(kept) != _iface_key(child):
                log.warning("同一接口 %s 在同一路径下有多份不同声明,保留首个", name)
    merged = ET.Element("node")
    for iface in by_name.values():
        merged.append(iface)
    return ET.tostring(merged, encoding="unicode")


def _iface_key(iface: ET.Element) -> str:
    """接口的规范化指纹:成员标签/名字/属性/参数,属性次序无关。"""
    parts: list[str] = []
    for member in iface:
        tag = local_name(member.tag)
        if tag not in ("method", "signal", "property"):
            continue
        args = "".join(
            f"{local_name(arg.tag)}{sorted(arg.attrib.items())}"
            for arg in member
            if local_name(arg.tag) == "arg"
        )
        parts.append(f"{tag}:{member.get('name', '')}:{sorted(member.attrib.items())}:{args}")
    return "|".join(parts)
