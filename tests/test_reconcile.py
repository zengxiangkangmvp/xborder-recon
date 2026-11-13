"""对账、报告与端到端演示测试。"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from xrecon.cli import app
from xrecon.ingest import load_settlement_csv
from xrecon.ledger import from_settlement
from xrecon.models import DIFF_KINDS, SettlementRow
from xrecon.reconcile import match
from xrecon.report import render_markdown, render_text
from xrecon.rules import Rules

D = Decimal
SOURCE = "abc123def456"


def _row(
    amount: str,
    *,
    order_id: str = "DEMO-ORDER-0001",
    sku: str = "DEMO-SKU-001",
    fee_type: str = "principal",
    period: str = "2026-01",
    line: int = 2,
) -> SettlementRow:
    return SettlementRow(
        marketplace="DEMO-MARKETPLACE",
        order_id=order_id,
        sku=sku,
        fee_type=fee_type,
        amount=D(amount),
        period=period,
        posted_at=date(2026, 1, 5),
        source_file=SOURCE,
        source_line=line,
    )


def _entries(rows: list[SettlementRow]):
    return from_settlement(rows, Rules.load())


def _load_demo_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "make_demo_data.py"
    spec = importlib.util.spec_from_file_location("_demo_data_for_tests", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_amount_mismatch_reports_delta_and_evidence() -> None:
    settlement = _row("10.00")
    ledger = _entries([_row("10.05")])
    diffs = match([settlement], ledger, Rules.load())
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.kind == "AMOUNT_MISMATCH"
    assert diff.settlement_total == D("10.00")
    assert diff.ledger_total == D("10.05")
    assert diff.delta == D("-0.05")
    assert any(item.startswith("settlement:") for item in diff.evidence)
    assert any(item.startswith("ledger:") for item in diff.evidence)


def test_tolerance_boundary_001_does_not_report() -> None:
    diffs = match([_row("10.00")], _entries([_row("10.01")]), Rules.load())
    assert diffs == []


def test_tolerance_boundary_002_reports() -> None:
    diffs = match([_row("10.00")], _entries([_row("10.02")]), Rules.load())
    assert len(diffs) == 1
    assert diffs[0].kind == "AMOUNT_MISMATCH"


def test_custom_tolerance_002_suppresses_002_difference() -> None:
    rules = Rules.model_validate({"version": 1, "tolerance": {"amount": "0.02"}})
    diffs = match([_row("10.00")], _entries([_row("10.02")]), rules)
    assert diffs == []


def test_timing_diff_when_amount_equal_but_period_differs() -> None:
    settlement = _row("-2.60", fee_type="commission", period="2026-01")
    ledger = _entries([_row("-2.60", fee_type="commission", period="2026-02")])
    diffs = match([settlement], ledger, Rules.load())
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.kind == "TIMING_DIFF"
    assert diff.delta == D("0.00")
    assert any(item.startswith("period:") for item in diff.evidence)


def test_missing_in_ledger() -> None:
    diffs = match([_row("28.90")], [], Rules.load())
    assert len(diffs) == 1
    assert diffs[0].kind == "MISSING_IN_LEDGER"
    assert diffs[0].ledger_total == D("0.00")
    assert diffs[0].delta == D("28.90")


def test_missing_in_settlement() -> None:
    ledger = _entries(
        [
            _row(
                "-4.20",
                order_id="DEMO-ORDER-0901",
                fee_type="commission",
                period="2026-02",
            )
        ]
    )
    diffs = match([], ledger, Rules.load())
    assert len(diffs) == 1
    assert diffs[0].kind == "MISSING_IN_SETTLEMENT"
    assert diffs[0].settlement_total == D("0.00")
    assert diffs[0].ledger_total == D("-4.20")
    assert diffs[0].delta == D("4.20")


def test_negative_amounts_round_trip_without_false_mismatch() -> None:
    rows = [_row("-2.60", fee_type="commission")]
    diffs = match(rows, _entries(rows), Rules.load())
    assert diffs == []


def test_same_key_multiple_rows_are_aggregated() -> None:
    settlement = [_row("3.33", line=2), _row("6.67", line=3)]
    ledger = _entries([_row("5.00", line=2), _row("5.00", line=3)])
    diffs = match(settlement, ledger, Rules.load())
    assert diffs == []


def test_aggregation_uses_signed_totals() -> None:
    settlement = [
        _row("-2.50", fee_type="commission", line=2),
        _row("-2.50", fee_type="commission", line=3),
    ]
    ledger = _entries([_row("-5.00", fee_type="commission")])
    assert match(settlement, ledger, Rules.load()) == []


def test_diff_within_tolerance_can_still_be_timing_diff() -> None:
    settlement = _row("10.00", period="2026-01")
    ledger = _entries([_row("10.01", period="2026-02")])
    diffs = match([settlement], ledger, Rules.load())
    assert len(diffs) == 1
    assert diffs[0].kind == "TIMING_DIFF"
    assert diffs[0].delta == D("-0.01")


def test_cross_month_amount_swap_reports_two_timing_diffs() -> None:
    settlement = [
        _row("-2.60", fee_type="commission", period="2026-01", line=2),
        _row("-9.90", fee_type="commission", period="2026-02", line=3),
    ]
    ledger = _entries(
        [
            _row("-9.90", fee_type="commission", period="2026-01", line=2),
            _row("-2.60", fee_type="commission", period="2026-02", line=3),
        ]
    )

    diffs = match(settlement, ledger, Rules.load())

    assert len(diffs) == 2
    assert {diff.kind for diff in diffs} == {"TIMING_DIFF"}
    by_period = {diff.period: diff.delta for diff in diffs}
    assert by_period == {"2026-01": D("7.30"), "2026-02": D("-7.30")}
    assert {abs(diff.delta) for diff in diffs} == {D("7.30")}


def test_same_key_same_period_multiple_rows_are_summed() -> None:
    settlement = [
        _row("3.33", period="2026-01", line=2),
        _row("6.67", period="2026-01", line=3),
    ]
    ledger = _entries(
        [
            _row("4.00", period="2026-01", line=2),
            _row("6.00", period="2026-01", line=3),
        ]
    )
    assert match(settlement, ledger, Rules.load()) == []


def test_diffs_carry_period_and_markdown_has_period_column() -> None:
    diffs = match([_row("10.00")], _entries([_row("10.05")]), Rules.load())
    assert diffs[0].period == "2026-01"
    markdown = render_markdown(
        diffs,
        rules_digest="rules1234567",
        settlement_digest="settle123456",
    )
    assert "| match_key | settlement | ledger | delta | evidence | period |" in markdown
    assert "| 2026-01 |" in markdown


def test_report_contains_run_info_and_grouped_tables() -> None:
    diffs = match([_row("10.00")], _entries([_row("10.05")]), Rules.load())
    markdown = render_markdown(
        diffs,
        rules_digest="rules1234567",
        settlement_digest="settle123456",
    )
    assert "rules1234567" in markdown
    assert "settle123456" in markdown
    assert "差异条数: **1**" in markdown
    assert "AMOUNT_MISMATCH" in markdown
    assert "| match_key | settlement | ledger | delta | evidence |" in markdown


def test_report_without_diffs_says_no_difference() -> None:
    markdown = render_markdown(
        [],
        rules_digest="rules1234567",
        settlement_digest="settle123456",
    )
    assert "无差异 ✅" in markdown
    assert "差异条数: **0**" in markdown


def test_text_report_without_diffs() -> None:
    report = render_text([], rules_digest="r", settlement_digest="s")
    assert "无差异 ✅" in report


def test_demo_data_produces_all_four_kinds(tmp_path: Path) -> None:
    module = _load_demo_module()
    settlement_path, ledger_path = module.generate_demo_data(tmp_path)
    settlement_rows = load_settlement_csv(settlement_path)
    ledger_rows = load_settlement_csv(ledger_path)
    rules = Rules.load()
    diffs = match(settlement_rows, _entries(ledger_rows), rules)

    assert len(settlement_rows) == 60
    assert len(ledger_rows) == 59
    assert {diff.kind for diff in diffs} == set(DIFF_KINDS)
    assert len(diffs) == 5
    assert all(row.sku.startswith("DEMO-SKU-") for row in settlement_rows)
    assert all(row.order_id.startswith("DEMO-2026-") for row in settlement_rows)


def test_cli_demo_exits_zero_and_shows_all_kinds() -> None:
    result = CliRunner().invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    for kind in DIFF_KINDS:
        assert kind in result.output
    assert "差异条数: **5**" in result.output


def test_cli_reconcile_json(tmp_path: Path) -> None:
    module = _load_demo_module()
    settlement_path, ledger_path = module.generate_demo_data(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "reconcile",
            str(settlement_path),
            "--ledger",
            str(ledger_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["diff_count"] == 5
    assert {item["kind"] for item in payload["diffs"]} == set(DIFF_KINDS)
    assert all("period" in item and item["period"] for item in payload["diffs"])
