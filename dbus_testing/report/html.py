"""report.html:单文件、零第三方依赖的人读报告.

Python 侧渲染静态表格 + 内联 CSS,并把 results.json 的内容以
<script type="application/json" id="results"> 内嵌,便于二次分析而不引入前端依赖。
所有动态文本一律经 html.escape。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from ..results import CaseResult, Delta, MemberCoverage, RunResult
from .jsonout import result_to_dict

_STATUS_LABEL = {
    "pass": "通过",
    "fail": "失败",
    "error": "错误",
    "config-error": "配置错误",
    "skip": "跳过",
}
_STATUS_CLASS = {
    "pass": "ok",
    "fail": "bad",
    "error": "bad",
    "config-error": "warn",
    "skip": "muted",
}
_DECLARED_LABEL = {
    "ok": "三方一致",
    "only-src": "仅源码声明",
    "only-baseline": "仅基线存在",
    "only-runtime": "仅运行时存在",
    "mismatch": "签名不一致",
    "no-src": "无源码声明",
    "unknown": "未知",
}
_KIND_LABEL = {"added": "新增", "removed": "删除", "changed": "变更"}

_CSS = """
body{margin:0;padding:24px;background:#f6f7f9;color:#1f2329;
font-family:"Noto Sans CJK SC","Source Han Sans SC",system-ui,sans-serif;font-size:14px}
h1{font-size:20px;margin:0 0 16px}
h2{font-size:17px;margin:28px 0 8px;padding-left:8px;border-left:4px solid #3a7afe}
h3{font-size:14px;margin:16px 0 4px;font-family:ui-monospace,monospace}
p{margin:4px 0 8px}
table{border-collapse:collapse;width:100%;background:#fff;margin:6px 0 18px}
th,td{border:1px solid #e3e6eb;padding:5px 8px;text-align:left;vertical-align:top;font-size:13px}
th{background:#eceff4;white-space:nowrap}
td.k{width:210px;color:#4b5563;white-space:nowrap}
.ok{color:#0a7a45;font-weight:600}
.bad{color:#c0392b;font-weight:600}
.warn{color:#b26a00;font-weight:600}
.muted{color:#6b7280}
.k-added{color:#0a7a45;font-weight:600}
.k-removed{color:#c0392b;font-weight:600}
.k-changed{color:#b26a00;font-weight:600}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:6px;padding:8px 16px;min-width:76px}
.card b{display:block;font-size:20px;line-height:1.4}
.card span{color:#6b7280;font-size:12px}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
pre{margin:0;white-space:pre-wrap;word-break:break-word}
tr.bad-row{background:#fdf3f2}
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _pct(part: int, total: int) -> str:
    return f"{part / total * 100:.1f}%" if total else "0.0%"


# --------------------------------------------------------------------------- 概要


def _env_table(result: RunResult) -> str:
    if not result.env:
        return '<p class="muted">无环境指纹。</p>'
    rows = "".join(
        f'<tr><td class="k">{_e(key)}</td><td><code>{_e(value)}</code></td></tr>'
        for key, value in result.env.items()
    )
    return (
        "<table><thead><tr><th>环境指纹</th><th>值</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _summary(result: RunResult) -> str:
    cards = [
        ("通过", result.passed, "ok" if result.passed else "muted"),
        ("失败", result.failed, "bad" if result.failed else "muted"),
        ("错误", result.errored, "bad" if result.errored else "muted"),
        ("配置错误", result.config_errors, "warn" if result.config_errors else "muted"),
        ("跳过", result.skipped, "muted"),
        ("flaky", result.flaky, "warn" if result.flaky else "muted"),
        ("用例总数", result.total, ""),
    ]
    parts = ["<h2>概要</h2>", '<div class="cards">']
    for label, count, cls in cards:
        parts.append(
            f'<div class="card"><b class="{cls}">{count}</b><span>{_e(label)}</span></div>'
        )
    parts.append(f'<div class="card"><b>{result.elapsed:.2f}s</b><span>总耗时</span></div>')
    parts.append("</div>")
    parts.append(_env_table(result))
    return "\n".join(parts)


# --------------------------------------------------------------------------- 覆盖矩阵


def _coverage_row(cov: MemberCoverage) -> str:
    declared_cls = {"ok": "ok", "mismatch": "bad"}.get(cov.declared, "warn")
    declared = _DECLARED_LABEL.get(cov.declared, cov.declared)
    if cov.executed_by:
        executed = f'<span class="ok">{_e("、".join(cov.executed_by))}</span>'
    else:
        executed = '<span class="bad">未覆盖</span>'
    return (
        f"<tr><td><code>{_e(cov.member)}</code></td><td>{_e(cov.kind)}</td>"
        f'<td class="{declared_cls}">{_e(declared)}'
        f' <span class="muted">({_e(cov.declared)})</span></td>'
        f"<td>{executed}</td></tr>"
    )


def _coverage_section(result: RunResult) -> str:
    matrix = result.coverage
    total = matrix.total
    declared_pct = _pct(matrix.declared_ok, total)
    executed_pct = _pct(matrix.executed_count, total)
    parts = [
        "<h2>接口覆盖矩阵</h2>",
        f'<p class="muted">声明覆盖 {declared_pct}({matrix.declared_ok}/{total})'
        f" · 执行覆盖 {executed_pct}({matrix.executed_count}/{total});"
        "声明覆盖=源码/基线/运行时三方一致,执行覆盖=确实被用例调用过。</p>",
    ]
    if not total:
        parts.append('<p class="muted">无覆盖数据。</p>')
        return "\n".join(parts)
    header = (
        "<table><thead><tr><th>成员</th><th>类型</th>"
        f"<th>声明覆盖 {declared_pct}</th><th>执行覆盖 {executed_pct}</th>"
        "</tr></thead><tbody>"
    )
    for interface in sorted(matrix.members):
        members = matrix.members[interface]
        parts.append(f"<h3>{_e(interface)}</h3>")
        parts.append(header)
        for cov in sorted(members.values(), key=lambda c: (c.kind, c.member)):
            parts.append(_coverage_row(cov))
        parts.append("</tbody></table>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- 用例明细


def _step_rows(case: CaseResult) -> str:
    if not case.steps:
        return '<tr><td colspan="6" class="muted">无步骤记录。</td></tr>'
    rows = []
    for index, step in enumerate(case.steps, start=1):
        target = step.step.target
        where = f"{target.path} {target.full_member}" if target else "-"
        mark = '<span class="ok">✓</span>' if step.ok else '<span class="bad">✗</span>'
        cls = "" if step.ok else ' class="bad-row"'
        rows.append(
            f"<tr{cls}><td>{index}</td><td><code>{_e(step.op)}</code></td>"
            f"<td><code>{_e(where)}</code></td><td>{mark}</td>"
            f"<td>{step.elapsed:.3f}s</td><td><pre>{_e(step.detail)}</pre></td></tr>"
        )
    return "".join(rows)


def _diagnosis_table(case: CaseResult) -> str:
    rows = [
        ("错误码", case.code or ""),
        ("摘要", case.summary),
        ("详情", case.detail),
        ("提示", case.hint),
    ]
    body = "".join(
        f'<tr><td class="k">{_e(label)}</td><td><pre>{_e(value)}</pre></td></tr>'
        for label, value in rows
        if value
    )
    if not body:
        return ""
    return f"<table><tbody>{body}</tbody></table>"


def _case_block(case: CaseResult) -> str:
    cls = _STATUS_CLASS.get(case.status, "warn")
    label = _STATUS_LABEL.get(case.status, case.status)
    meta = [f"tests.yaml:{case.case.line}", f"耗时 {case.elapsed:.3f}s"]
    if case.attempts > 1:
        meta.append(f"尝试 {case.attempts} 次")
    if case.flaky:
        meta.append("flaky")
    if case.case.skip:
        meta.append(f"跳过原因: {case.case.skip}")
    parts = [
        f'<h3>{_e(case.name)} <span class="{cls}">[{_e(label)}]</span></h3>',
        f'<p class="muted">{_e(" · ".join(meta))}</p>',
    ]
    if case.status != "pass":
        parts.append(_diagnosis_table(case))
    parts.append(
        "<table><thead><tr><th>#</th><th>操作</th><th>目标</th><th>结果</th>"
        "<th>耗时</th><th>详情</th></tr></thead>"
        f"<tbody>{_step_rows(case)}</tbody></table>"
    )
    return "\n".join(p for p in parts if p)


def _cases_section(result: RunResult) -> str:
    parts = ["<h2>用例明细</h2>"]
    if not result.cases:
        parts.append('<p class="muted">未执行任何用例。</p>')
    parts.extend(_case_block(case) for case in result.cases)
    return "\n".join(parts)


# --------------------------------------------------------------------------- 契约差异


def _delta_table(title: str, deltas: list[Delta], empty: str) -> str:
    parts = [f"<h3>{_e(title)}</h3>"]
    if not deltas:
        parts.append(f'<p class="muted">{_e(empty)}</p>')
        return "\n".join(parts)
    rows = []
    for delta in deltas:
        rows.append(
            f'<tr><td class="k-{_e(delta.kind)}">{_e(_KIND_LABEL.get(delta.kind, delta.kind))}'
            f" ({_e(delta.kind)})</td><td>{_e(delta.scope)}</td>"
            f"<td><code>{_e(delta.path)}</code></td><td><code>{_e(delta.interface)}</code></td>"
            f"<td><code>{_e(delta.member or '-')}</code></td>"
            f"<td><pre>{_e(delta.before or '-')}</pre></td>"
            f"<td><pre>{_e(delta.after or '-')}</pre></td></tr>"
        )
    parts.append(
        "<table><thead><tr><th>类别</th><th>范围</th><th>对象路径</th><th>接口</th>"
        "<th>成员</th><th>变更前</th><th>变更后</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return "\n".join(parts)


def _contract_section(result: RunResult) -> str:
    static_empty = result.static_skipped or "无源码声明漂移。"
    return "\n".join(
        [
            "<h2>契约差异</h2>",
            _delta_table("运行时 vs 基线(contract)", result.contract, "无运行时漂移。"),
            _delta_table(
                "源码声明 vs 基线(static_contract)", result.static_contract, static_empty
            ),
        ]
    )


# --------------------------------------------------------------------------- 渲染入口


def _embedded_json(result: RunResult) -> str:
    payload = json.dumps(result_to_dict(result), indent=2, ensure_ascii=False)
    # "</" 会提前终止 <script> 块;\/ 是合法 JSON 转义,解析端拿到的字符串不变
    return payload.replace("</", "<\\/")


def write_html(result: RunResult, out_path: Path) -> None:
    verdict = ("ok", "整体通过") if result.ok else ("bad", "整体未通过")
    doc = "\n".join(
        [
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>',
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>DBus 接口测试报告</title>",
            f"<style>{_CSS}</style>",
            "</head>\n<body>",
            f'<h1>DBus 接口测试报告 <span class="{verdict[0]}">[{verdict[1]}]</span></h1>',
            _summary(result),
            _coverage_section(result),
            _cases_section(result),
            _contract_section(result),
            '<script type="application/json" id="results">',
            _embedded_json(result),
            "</script>",
            "</body>\n</html>",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc + "\n", encoding="utf-8")
