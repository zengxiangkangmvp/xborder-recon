"""对账报告渲染。"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .models import DIFF_KINDS, Diff

__all__ = ["render_markdown", "render_text"]

_KIND_LABELS = {
    "AMOUNT_MISMATCH": "金额差",
    "TIMING_DIFF": "跨月归属差",
    "MISSING_IN_LEDGER": "账本缺失",
    "MISSING_IN_SETTLEMENT": "账本多记",
}


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _header(*, rules_digest: str, settlement_digest: str, diff_count: int) -> list[str]:
    return [
        "# 跨境对账报告",
        "",
        f"- rules digest: `{rules_digest}`",
        f"- settlement digest: `{settlement_digest}`",
        f"- 差异条数: **{diff_count}**",
        "",
    ]


def render_markdown(
    diffs: Iterable[Diff],
    *,
    rules_digest: str,
    settlement_digest: str,
) -> str:
    """输出按差异类型分组的 Markdown 报告。"""

    diff_list = list(diffs)
    lines = _header(
        rules_digest=rules_digest,
        settlement_digest=settlement_digest,
        diff_count=len(diff_list),
    )

    if not diff_list:
        lines.extend(["## 对账结果", "", "无差异 ✅", ""])
        return "\n".join(lines)

    lines.extend(["## 差异汇总", ""])
    for kind in DIFF_KINDS:
        count = sum(1 for diff in diff_list if diff.kind == kind)
        if count:
            lines.append(f"- {_KIND_LABELS[kind]}（`{kind}`）：{count} 条")
    lines.append("")

    for kind in DIFF_KINDS:
        group = [diff for diff in diff_list if diff.kind == kind]
        if not group:
            continue
        lines.extend(
            [
                f"## {kind} · {_KIND_LABELS[kind]}（{len(group)} 条）",
                "",
                "| match_key | settlement | ledger | delta | evidence | period |",
                "| --- | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for diff in group:
            evidence = "<br>".join(f"`{_cell(item)}`" for item in diff.evidence)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_cell(diff.match_key.display())}`",
                        _money(diff.settlement_total),
                        _money(diff.ledger_total),
                        _money(diff.delta),
                        evidence,
                        _cell(diff.period or ""),
                    ]
                )
                + " |"
            )
        lines.append("")

    return "\n".join(lines)


def render_text(
    diffs: Iterable[Diff],
    *,
    rules_digest: str,
    settlement_digest: str,
) -> str:
    """终端友好的纯文本报告。"""

    diff_list = list(diffs)
    lines = [
        "跨境对账报告",
        f"rules digest: {rules_digest}",
        f"settlement digest: {settlement_digest}",
        f"差异条数: {len(diff_list)}",
        "",
    ]
    if not diff_list:
        lines.append("无差异 ✅")
        return "\n".join(lines)

    for diff in diff_list:
        lines.append(
            f"[{diff.kind}] {diff.match_key.display()} "
            f"period={diff.period or ''} "
            f"settlement={diff.settlement_total:.2f} "
            f"ledger={diff.ledger_total:.2f} delta={diff.delta:.2f}"
        )
        for item in diff.evidence:
            lines.append(f"  - {item}")
    return "\n".join(lines)
