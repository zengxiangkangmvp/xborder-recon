"""账本、Money 与规则分摊测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from xrecon.ledger import Ledger, LedgerImbalanceError, from_settlement
from xrecon.models import LedgerEntry, MatchKey, Money, SettlementRow
from xrecon.rules import Rules

D = Decimal


def _row(
    amount: str,
    *,
    order_id: str = "DEMO-ORDER-0001",
    sku: str = "DEMO-SKU-001",
    fee_type: str = "principal",
    line: int = 2,
) -> SettlementRow:
    return SettlementRow(
        marketplace="DEMO-MARKETPLACE",
        order_id=order_id,
        sku=sku,
        fee_type=fee_type,
        amount=D(amount),
        period="2026-01",
        posted_at=date(2026, 1, 5),
        source_file="abc123def456",
        source_line=line,
    )


def _key() -> MatchKey:
    return MatchKey("DEMO-MARKETPLACE", "DEMO-SKU-001", "DEMO-ORDER-0001", "principal")


def test_money_rounds_1_005_half_up() -> None:
    assert Money("1.005").amount == D("1.01")
    assert Money("1.005").value == D("1.01")


def test_money_rounds_negative_half_away_from_zero() -> None:
    assert Money("-1.005").amount == D("-1.01")


def test_money_arithmetic_returns_quantized_money() -> None:
    result = Money("0.10") + Money("0.20")
    assert isinstance(result, Money)
    assert result.amount == D("0.30")
    assert str(Money("2") + D("0.005")) == "2.01"


def test_post_creates_balanced_entry() -> None:
    ledger = Ledger()
    entry = ledger.post(
        period="2026-01",
        memo="测试收入",
        debit_account="platform_receivable",
        credit_account="revenue:principal",
        amount=D("10.005"),
        source_ref="abc123def456:2",
        match_key=_key(),
    )
    assert len(entry.entry_id) == 12
    assert entry.amount == D("10.01")
    assert entry.debit_account == "platform_receivable"
    assert ledger.balance() == D("0.00")


def test_post_rejects_non_positive_amount() -> None:
    ledger = Ledger()
    with pytest.raises(ValueError, match="大于 0"):
        ledger.post(
            "2026-01",
            "零金额",
            "platform_receivable",
            "revenue:principal",
            D("0"),
            "abc123def456:2",
            _key(),
        )


def test_balance_raises_for_damaged_entry() -> None:
    ledger = Ledger()
    ledger.post(
        "2026-01",
        "正常分录",
        "platform_receivable",
        "revenue:principal",
        D("10.00"),
        "abc123def456:2",
        _key(),
    )
    # 人为破坏：贷方账户为空，模拟分录被错误编辑。
    ledger.add(
        LedgerEntry(
            entry_id="broken000001",
            period="2026-01",
            memo="损坏分录",
            debit_account="platform_receivable",
            credit_account="",
            amount=D("99.00"),
            source_ref="abc123def456:3",
            match_key=_key(),
        )
    )
    with pytest.raises(LedgerImbalanceError, match="缺少借方或贷方"):
        ledger.balance()


def test_balance_raises_for_same_debit_and_credit_account() -> None:
    ledger = Ledger(
        [
            LedgerEntry(
                entry_id="broken000002",
                period="2026-01",
                memo="借贷同户",
                debit_account="platform_receivable",
                credit_account="platform_receivable",
                amount=D("1.00"),
                source_ref="abc123def456:2",
                match_key=_key(),
            )
        ]
    )
    with pytest.raises(LedgerImbalanceError, match="借贷同户"):
        ledger.balance()


def test_from_settlement_maps_positive_and_negative_accounts() -> None:
    rows = [
        _row("10.00", order_id="DEMO-ORDER-0001", fee_type="principal", line=2),
        _row("-2.50", order_id="DEMO-ORDER-0002", fee_type="commission", line=3),
    ]
    entries = from_settlement(rows, Rules.load())
    assert entries[0].debit_account == "platform_receivable"
    assert entries[0].credit_account == "revenue:principal"
    assert entries[0].amount == D("10.00")
    assert entries[0].signed_amount == D("10.00")
    assert entries[1].debit_account == "expense:commission"
    assert entries[1].credit_account == "platform_receivable"
    assert entries[1].amount == D("2.50")
    assert entries[1].signed_amount == D("-2.50")
    assert entries[0].source_ref == "abc123def456:2"


def test_from_settlement_entries_balance_and_have_unique_ids() -> None:
    rows = [_row("10.00", line=2), _row("-1.25", order_id="DEMO-ORDER-0002", line=3)]
    entries = from_settlement(rows, Rules.load())
    ledger = Ledger(entries)
    assert ledger.balance() == D("0.00")
    assert len({entry.entry_id for entry in entries}) == 2


def test_allocate_ad_cost_indivisible_total_is_preserved() -> None:
    rules = Rules.load()
    revenue = {
        "DEMO-SKU-001": D("10.00"),
        "DEMO-SKU-002": D("10.00"),
        "DEMO-SKU-003": D("10.00"),
    }
    allocation = rules.allocate_ad_cost(D("100.00"), revenue)
    assert sum(allocation.values()) == D("100.00")
    assert allocation == {
        "DEMO-SKU-001": D("33.34"),
        "DEMO-SKU-002": D("33.33"),
        "DEMO-SKU-003": D("33.33"),
    }


def test_allocate_ad_cost_falls_back_to_equal_when_any_revenue_zero() -> None:
    rules = Rules.load()
    revenue = {
        "DEMO-SKU-001": D("0.00"),
        "DEMO-SKU-002": D("30.00"),
        "DEMO-SKU-003": D("60.00"),
    }
    allocation = rules.allocate_ad_cost(D("10.00"), revenue)
    assert sum(allocation.values()) == D("10.00")
    assert set(allocation) == set(revenue)
    assert max(allocation.values()) - min(allocation.values()) <= D("0.01")


def test_allocate_ad_cost_all_zero_revenue_is_equal() -> None:
    rules = Rules.load()
    revenue = {"DEMO-SKU-001": D("0"), "DEMO-SKU-002": D("0")}
    allocation = rules.allocate_ad_cost(D("0.03"), revenue)
    assert sum(allocation.values()) == D("0.03")
    assert allocation == {"DEMO-SKU-001": D("0.01"), "DEMO-SKU-002": D("0.02")}


def test_allocate_ad_cost_manual_weights(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """
version: 1
allocation:
  ad_cost:
    method: manual
    manual:
      DEMO-SKU-001: 2
      DEMO-SKU-002: 1
""",
        encoding="utf-8",
    )
    rules = Rules.load(rules_path)
    allocation = rules.allocate_ad_cost(
        D("10.00"),
        {"DEMO-SKU-001": D("1"), "DEMO-SKU-002": D("1")},
    )
    assert allocation == {"DEMO-SKU-001": D("6.67"), "DEMO-SKU-002": D("3.33")}


def test_rules_digest_is_stable_and_12_hex_chars() -> None:
    first = Rules.load().digest()
    second = Rules.load().digest()
    assert first == second
    assert len(first) == 12
    int(first, 16)  # 必须是合法十六进制


def test_rules_digest_changes_with_tolerance(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        'version: 1\ntolerance:\n  amount: "0.02"\n',
        encoding="utf-8",
    )
    changed = Rules.load(rules_path)
    assert changed.tolerance.amount == D("0.02")
    assert changed.digest() != Rules.load().digest()
