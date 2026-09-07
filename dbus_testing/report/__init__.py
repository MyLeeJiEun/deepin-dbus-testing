"""报告输出:junit.xml(CI 门禁) / results.json(机器可读) / report.html(人读)。"""

from __future__ import annotations

from .html import write_html
from .jsonout import result_to_dict, write_json
from .junit import write_junit

__all__ = ["result_to_dict", "write_html", "write_json", "write_junit"]
