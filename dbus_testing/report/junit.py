"""junit.xml:CI 门禁直接消费的标准 JUnit 报告.

用 ElementTree 构建,XML 特殊字符由它负责转义;控制字符本模块先剔除,
否则被测进程 stderr 里的裸 \\x00 之类会让 CI 端的 XML 解析器整份读不出来。
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..results import CaseResult, RunResult

SUITE_NAME = "dbus-interface"

# XML 1.0 不允许的字符(除 \t \n \r 外的 C0 控制符与 DEL)
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value: object) -> str:
    return _ILLEGAL.sub("", str(value))


def _time(seconds: float) -> str:
    return f"{seconds:.3f}"


def _classname(result: RunResult) -> str:
    """classname 取主服务名,CI 上按服务聚合;环境指纹缺失时退回套件名。"""
    for key in ("service", "services", "primary-service"):
        raw = result.env.get(key, "").strip()
        if raw:
            return raw.replace(",", " ").split()[0]
    return SUITE_NAME


def _diagnostic(node: ET.Element, tag: str, case: CaseResult) -> None:
    """失败/错误正文:summary 进 message,code 进 type,detail 与 hint 进正文。"""
    el = ET.SubElement(
        node,
        tag,
        {"message": _text(case.summary or case.status), "type": _text(case.code or "")},
    )
    body = [case.detail]
    if case.hint:
        body.append(f"提示: {case.hint}")
    el.text = _text("\n\n".join(p for p in body if p))


def _fill_case(node: ET.Element, case: CaseResult) -> None:
    if case.status == "skip":
        skipped = ET.SubElement(node, "skipped")
        reason = case.case.skip or case.summary
        if reason:
            skipped.set("message", _text(reason))
    elif case.status in ("error", "config-error"):
        # 护栏拒绝与环境错误都不是用例失败,按 JUnit 的 error 归类
        _diagnostic(node, "error", case)
    elif case.status == "fail":
        _diagnostic(node, "failure", case)
    if case.flaky:
        ET.SubElement(node, "system-out").text = f"flaky: attempts={case.attempts}"


def write_junit(result: RunResult, out_path: Path) -> None:
    root = ET.Element(
        "testsuite",
        {
            "name": SUITE_NAME,
            "tests": str(result.total),
            "failures": str(result.failed),
            "errors": str(result.errored + result.config_errors),
            "skipped": str(result.skipped),
            "time": _time(result.elapsed),
        },
    )
    if result.env:
        props = ET.SubElement(root, "properties")
        for key, value in result.env.items():
            ET.SubElement(props, "property", {"name": _text(key), "value": _text(value)})
    classname = _classname(result)
    for case in result.cases:
        node = ET.SubElement(
            root,
            "testcase",
            {"classname": classname, "name": _text(case.name), "time": _time(case.elapsed)},
        )
        _fill_case(node, case)
    ET.indent(root, space="  ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
