"""运行时接口快照:遍历对象树并抓取 introspection XML。

与 ignore.paths 的配合(实现要点):动态子对象(ObjectManager 模式)常有成百上千个兄弟路径。
逐模式只保留**第一个**命中作为样本(walk 的 "prune":introspect 但不向下展开),
其余兄弟直接 "skip"。这样规范化阶段能把它们归并成一个 collapsed 节点,
基线里看得见这一节,又不会把整棵动态子树都 introspect 一遍。
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase

from ..core.client import BusClient
from ..model import IgnoreSpec

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
    """抓取给定服务名下的全部对象与其 introspection XML。"""
    tree: ObjectTree = {}
    for service in services:
        decide = make_decider(ignore)
        for path, xml in client.walk(service, root, decide=decide):  # type: ignore[arg-type]
            if path in tree:
                continue
            tree[path] = xml
    log.debug("运行时快照: %d 个对象", len(tree))
    return tree
