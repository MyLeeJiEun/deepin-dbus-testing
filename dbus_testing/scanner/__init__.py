"""scanner:接口声明的采集、规范化与比对。

- normalize:运行时 introspection / 源码声明统一成一份可逐字符比对的规范化 XML;
- diff:规范化 XML 之间的漂移清单(运行时 vs 基线);
- srcxml:只读收集仓内源码声明 XML,并给出静态比对(源码 vs 基线)。
"""

from .diff import diff
from .normalize import normalize_interface_xml, normalize_tree, parse_members
from .srcxml import collect_src_xml, src_baseline_xml, static_diff

__all__ = [
    "collect_src_xml",
    "diff",
    "normalize_interface_xml",
    "normalize_tree",
    "parse_members",
    "src_baseline_xml",
    "static_diff",
]
