"""对账差异计算。"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from .models import (
    DIFF_KINDS,
    Diff,
    LedgerEntry,
    MatchKey,
    SettlementRow,
    quantize_money,
    to_decimal,
)
from .rules import Rules

__all__ = ["match"]

_KIND_ORDER = {kind: index for index, kind in enumerate(DIFF_KINDS)}


def _resolve_tolerance(
    rules: Rules | Decimal | None,
    tolerance: Decimal | None,
) -> Decimal:
    if tolerance is not None:
        return abs(to_decimal(tolerance))
    if isinstance(rules, Decimal):
        return abs(rules)
    if rules is None:
        rules = Rules.load()
    return abs(to_decimal(rules.tolerance.amount))


def _sum_settlement(rows: Iterable[SettlementRow]) -> Decimal:
    return quantize_money(sum((row.amount for row in rows), Decimal("0.00")))


def _sum_ledger(entries: Iterable[LedgerEntry]) -> Decimal:
    # 分录金额恒为正，这里按借贷账户方向还原为业务金额（收入正、支出负）。
    return quantize_money(
        sum((entry.signed_amount for entry in entries), Decimal("0.00"))
    )


def _settlement_evidence(rows: Iterable[SettlementRow]) -> list[str]:
    refs = [f"settlement:{row.source_file}:{row.source_line}" for row in rows]
    return list(dict.fromkeys(refs))


def _ledger_evidence(entries: Iterable[LedgerEntry]) -> list[str]:
    refs = [f"ledger:{entry.entry_id}" for entry in entries]
    return list(dict.fromkeys(refs))


def _period_evidence(
    settlement_periods: Iterable[str],
    ledger_periods: Iterable[str],
) -> str:
    return (
        "period:settlement="
        + ",".join(sorted(settlement_periods))
        + "|ledger="
        + ",".join(sorted(ledger_periods))
    )


def _amount_matches(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(quantize_money(left - right)) <= tolerance


def _match_periods(
    settlement_periods: Sequence[str],
    ledger_periods: Sequence[str],
    settlement_totals: Mapping[str, Decimal],
    ledger_totals: Mapping[str, Decimal],
    tolerance: Decimal,
    *,
    require_all_ledger: bool,
) -> dict[str, str]:
    """按金额把结算期间与账本期间配对，返回 ``结算期间 -> 账本期间``。

    使用增广路径求最大二分匹配。金额相同但月份不同的配对即跨月归属；
    ``require_all_ledger`` 为真时只有账本期间也全部配完才返回结果。
    """

    candidates = {
        settlement_period: [
            ledger_period
            for ledger_period in ledger_periods
            if _amount_matches(
                settlement_totals[settlement_period],
                ledger_totals[ledger_period],
                tolerance,
            )
        ]
        for settlement_period in settlement_periods
    }

    ledger_to_settlement: dict[str, str] = {}

    def _augment(settlement_period: str, seen: set[str]) -> bool:
        for ledger_period in candidates[settlement_period]:
            if ledger_period in seen:
                continue
            seen.add(ledger_period)
            current = ledger_to_settlement.get(ledger_period)
            if current is None or _augment(current, seen):
                ledger_to_settlement[ledger_period] = settlement_period
                return True
        return False

    for settlement_period in settlement_periods:
        _augment(settlement_period, set())

    matches = {
        settlement_period: ledger_period
        for ledger_period, settlement_period in ledger_to_settlement.items()
    }
    if require_all_ledger and len(matches) != len(settlement_periods):
        return {}
    return matches


def _missing_in_ledger(
    key: MatchKey,
    period: str,
    rows: Sequence[SettlementRow],
    total: Decimal,
) -> Diff:
    return Diff(
        kind="MISSING_IN_LEDGER",
        match_key=key,
        settlement_total=total,
        ledger_total=Decimal("0.00"),
        delta=total,
        evidence=_settlement_evidence(rows),
        period=period,
    )


def _missing_in_settlement(
    key: MatchKey,
    period: str,
    entries: Sequence[LedgerEntry],
    total: Decimal,
) -> Diff:
    return Diff(
        kind="MISSING_IN_SETTLEMENT",
        match_key=key,
        settlement_total=Decimal("0.00"),
        ledger_total=total,
        delta=-total,
        evidence=_ledger_evidence(entries),
        period=period,
    )


def match(
    settlement: Iterable[SettlementRow],
    ledger: Iterable[LedgerEntry],
    rules: Rules | Decimal | None = None,
    *,
    tolerance: Decimal | None = None,
) -> list[Diff]:
    """按 ``(MatchKey, period)`` 聚合两侧金额并返回四类差异。

    金额差判据为同一期间内 ``abs(settlement_total - ledger_total) > tolerance``，
    因此默认容差 0.01 时，正好差 0.01 不报、差 0.02 报。

    同一 ``MatchKey`` 在结算与账本之间跨月互换金额时，两期金额可以一一配对，
    按期间分别报 ``TIMING_DIFF``；只有确实无法归因到另一期间的金额，才报
    金额差或单边缺失。
    """

    tolerance_value = _resolve_tolerance(rules, tolerance)

    settlement_groups: dict[MatchKey, dict[str, list[SettlementRow]]] = defaultdict(
        lambda: defaultdict(list)
    )
    ledger_groups: dict[MatchKey, dict[str, list[LedgerEntry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in settlement:
        settlement_groups[row.match_key][row.period].append(row)
    for entry in ledger:
        ledger_groups[entry.match_key][entry.period].append(entry)

    diffs: list[Diff] = []
    for key in sorted(set(settlement_groups) | set(ledger_groups)):
        settlement_by_period = settlement_groups.get(key, {})
        ledger_by_period = ledger_groups.get(key, {})
        settlement_totals = {
            period: _sum_settlement(rows)
            for period, rows in settlement_by_period.items()
        }
        ledger_totals = {
            period: _sum_ledger(entries)
            for period, entries in ledger_by_period.items()
        }

        settlement_periods = set(settlement_totals)
        ledger_periods = set(ledger_totals)
        common_periods = settlement_periods & ledger_periods
        settlement_only = sorted(settlement_periods - common_periods)
        ledger_only = sorted(ledger_periods - common_periods)

        # 同期间且金额在容差内：正常匹配，不产生差异。
        mismatched_common = sorted(
            period
            for period in common_periods
            if not _amount_matches(
                settlement_totals[period],
                ledger_totals[period],
                tolerance_value,
            )
        )

        # 单边期间：金额能跨月配对的报 TIMING_DIFF，其余按缺失处理。
        one_sided_pairs = _match_periods(
            settlement_only,
            ledger_only,
            settlement_totals,
            ledger_totals,
            tolerance_value,
            require_all_ledger=False,
        )
        paired_ledger_periods = set(one_sided_pairs.values())
        for settlement_period, ledger_period in sorted(one_sided_pairs.items()):
            settlement_total = settlement_totals[settlement_period]
            ledger_total = ledger_totals[ledger_period]
            diffs.append(
                Diff(
                    kind="TIMING_DIFF",
                    match_key=key,
                    settlement_total=settlement_total,
                    ledger_total=ledger_total,
                    delta=quantize_money(settlement_total - ledger_total),
                    evidence=_settlement_evidence(
                        settlement_by_period[settlement_period]
                    )
                    + _ledger_evidence(ledger_by_period[ledger_period])
                    + [
                        _period_evidence(
                            [settlement_period],
                            [ledger_period],
                        )
                    ],
                    period=settlement_period,
                )
            )

        for period in settlement_only:
            if period in one_sided_pairs:
                continue
            rows = settlement_by_period[period]
            diffs.append(
                _missing_in_ledger(
                    key,
                    period,
                    rows,
                    settlement_totals[period],
                )
            )

        for period in ledger_only:
            if period in paired_ledger_periods:
                continue
            entries = ledger_by_period[period]
            diffs.append(
                _missing_in_settlement(
                    key,
                    period,
                    entries,
                    ledger_totals[period],
                )
            )

        # 两侧都有但同月金额不一致：如果整个 key 的差异月份可以按金额做一次
        # 完整换位（如两个月金额互换），说明是归属期间错了，按期间报跨月差异；
        # 否则是真正的金额差。
        swap_pairs = _match_periods(
            mismatched_common,
            mismatched_common,
            settlement_totals,
            ledger_totals,
            tolerance_value,
            require_all_ledger=True,
        ) if mismatched_common else {}
        is_timing_swap = bool(swap_pairs) and len(swap_pairs) == len(
            mismatched_common
        )

        for period in mismatched_common:
            settlement_total = settlement_totals[period]
            ledger_total = ledger_totals[period]
            settlement_rows = settlement_by_period[period]
            ledger_entries = ledger_by_period[period]
            evidence = _settlement_evidence(settlement_rows) + _ledger_evidence(
                ledger_entries
            )
            if is_timing_swap:
                diffs.append(
                    Diff(
                        kind="TIMING_DIFF",
                        match_key=key,
                        settlement_total=settlement_total,
                        ledger_total=ledger_total,
                        delta=quantize_money(settlement_total - ledger_total),
                        evidence=evidence
                        + [_period_evidence([period], [period])],
                        period=period,
                    )
                )
            else:
                diffs.append(
                    Diff(
                        kind="AMOUNT_MISMATCH",
                        match_key=key,
                        settlement_total=settlement_total,
                        ledger_total=ledger_total,
                        delta=quantize_money(settlement_total - ledger_total),
                        evidence=evidence,
                        period=period,
                    )
                )

    diffs.sort(
        key=lambda diff: (
            _KIND_ORDER[diff.kind],
            diff.match_key,
            diff.period or "",
        )
    )
    return diffs
