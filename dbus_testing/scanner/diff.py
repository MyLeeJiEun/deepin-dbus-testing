"""规范化 XML 的契约比对。

比对维度自外向内:对象路径集合(scope="path")→ 接口集合(scope="interface")→
成员集合与签名(scope=method/signal/property)。路径或接口整体增删只报一条 Delta,
不再展开其下成员 —— 报告要的是"哪里变了",逐成员铺开只会淹没信号。

输出天然按 (path, interface, member) 稳定排序:各层都走 sorted(),同层内按插入序。
两侧输入都必须是 normalize 的输出(ignore 已在规范化阶段生效)。
"""

from __future__ import annotations

from typing import Literal

from ..results import Delta
from .normalize import MemberTable, member_index

DeltaKind = Literal["added", "removed", "changed"]


def diff_members(path: str, interface: str, before: MemberTable, after: MemberTable) -> list[Delta]:
    """比对同一接口的成员表。同名不同 kind(如 method 改成 signal)记为 removed + added。"""
    deltas: list[Delta] = []
    for key in sorted(set(before) | set(after), key=lambda k: (k[1], k[0])):
        scope, member = key
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        kind: DeltaKind = "added" if old is None else "removed" if new is None else "changed"
        deltas.append(
            Delta(
                kind=kind,
                scope=scope,
                path=path,
                interface=interface,
                member=member,
                before=old,
                after=new,
            )
        )
    return deltas


def diff(baseline_xml: str, actual_xml: str) -> list[Delta]:
    """基线 -> 实际的漂移清单;空列表即契约一致。"""
    baseline = member_index(baseline_xml)
    actual = member_index(actual_xml)
    deltas: list[Delta] = []
    for path in sorted(set(baseline) | set(actual)):
        if path not in baseline or path not in actual:
            kind: DeltaKind = "removed" if path in baseline else "added"
            deltas.append(Delta(kind=kind, scope="path", path=path, interface=""))
            continue
        old_ifaces, new_ifaces = baseline[path], actual[path]
        for interface in sorted(set(old_ifaces) | set(new_ifaces)):
            if interface in old_ifaces and interface in new_ifaces:
                deltas.extend(
                    diff_members(path, interface, old_ifaces[interface], new_ifaces[interface])
                )
                continue
            iface_kind: DeltaKind = "removed" if interface in old_ifaces else "added"
            deltas.append(
                Delta(kind=iface_kind, scope="interface", path=path, interface=interface)
            )
    return deltas
