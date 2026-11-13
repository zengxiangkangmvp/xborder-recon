"""双分录账本。

每条结算行会生成一条借贷金额相等的分录。账本自身还要承担审计职责：
空账户、借贷同户、非正金额都会被 ``balance()`` 视为损坏并拒绝。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable
from uuid import uuid4

from .models import LedgerEntry, MatchKey, Money, SettlementRow, quantize_money

__all__ = [
    "Ledger",
    "LedgerImbalanceError",
    "from_settlement",
]


class LedgerImbalanceError(RuntimeError):
    """账本借贷不平衡或分录结构损坏。"""


class Ledger:
    """内存双分录账本。"""

    def __init__(self, entries: Iterable[LedgerEntry] | None = None) -> None:
        self._entries: list[LedgerEntry] = list(entries or [])

    @property
    def entries(self) -> list[LedgerEntry]:
        """返回内部列表，便于审计和测试时注入损坏分录。"""

        return self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def post(
        self,
        period: str,
        memo: str,
        debit_account: str,
        credit_account: str,
        amount: Decimal | Money,
        source_ref: str,
        match_key: MatchKey,
        *,
        entry_id: str | None = None,
    ) -> LedgerEntry:
        """登记一条双分录；金额必须大于 0。"""

        normalized = quantize_money(amount.amount if isinstance(amount, Money) else amount)
        if normalized <= 0:
            raise ValueError(f"分录金额必须大于 0，收到 {normalized}")
        if not debit_account or not credit_account:
            raise ValueError("借贷账户不能为空")
        if debit_account == credit_account:
            raise ValueError(f"借方与贷方不能是同一账户: {debit_account}")

        entry = LedgerEntry(
            entry_id=entry_id or uuid4().hex[:12],
            period=period,
            memo=memo,
            debit_account=debit_account,
            credit_account=credit_account,
            amount=normalized,
            source_ref=source_ref,
            match_key=match_key,
        )
        self._entries.append(entry)
        return entry

    def add(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)

    def extend(self, entries: Iterable[LedgerEntry]) -> None:
        self._entries.extend(entries)

    def balance(self) -> Decimal:
        """返回借方合计减贷方合计；不平衡时抛 ``LedgerImbalanceError``。"""

        debit_total = Decimal("0.00")
        credit_total = Decimal("0.00")

        for index, entry in enumerate(self._entries):
            if not entry.debit_account or not entry.credit_account:
                raise LedgerImbalanceError(
                    f"第 {index + 1} 条分录 {entry.entry_id} 缺少借方或贷方账户"
                )
            if entry.debit_account == entry.credit_account:
                raise LedgerImbalanceError(
                    f"第 {index + 1} 条分录 {entry.entry_id} 借贷同户: "
                    f"{entry.debit_account}"
                )
            if entry.amount <= 0:
                raise LedgerImbalanceError(
                    f"第 {index + 1} 条分录 {entry.entry_id} 金额非正: {entry.amount}"
                )
            debit_total += entry.amount
            credit_total += entry.amount

        difference = quantize_money(debit_total - credit_total)
        if difference != 0:
            raise LedgerImbalanceError(
                f"账本不平衡：借方 {debit_total:.2f} - 贷方 {credit_total:.2f} "
                f"= {difference:.2f}"
            )
        return difference

    def totals(self) -> tuple[Decimal, Decimal]:
        """返回（借方合计，贷方合计），同样执行结构校验。"""

        self.balance()
        debit_total = sum((entry.amount for entry in self._entries), Decimal("0.00"))
        return quantize_money(debit_total), quantize_money(debit_total)

    @staticmethod
    def from_settlement(
        rows: Iterable[SettlementRow],
        rules: object | None = None,
    ) -> list[LedgerEntry]:
        """类方法风格的便捷入口，等价于模块级 ``from_settlement``。"""

        return from_settlement(rows, rules)


def from_settlement(
    rows: Iterable[SettlementRow],
    rules: object | None = None,
) -> list[LedgerEntry]:
    """把结算行转换成双分录。

    - 正数收入：借 ``platform_receivable``，贷 ``revenue:{fee_type}``
    - 负数支出：借 ``expense:{fee_type}``，贷 ``platform_receivable``

    零金额行对账本没有影响，直接跳过。``rules`` 为后续汇率/归属扩展保留。
    """

    del rules  # v0.1 的账户映射是固定口径，规则参数用于调用方保持前向兼容
    ledger = Ledger()
    for row in rows:
        amount = quantize_money(row.amount)
        if amount == 0:
            continue
        if amount > 0:
            debit_account = "platform_receivable"
            credit_account = f"revenue:{row.fee_type}"
        else:
            debit_account = f"expense:{row.fee_type}"
            credit_account = "platform_receivable"

        ledger.post(
            period=row.period,
            memo=f"结算 {row.order_id}/{row.sku}/{row.fee_type}",
            debit_account=debit_account,
            credit_account=credit_account,
            amount=abs(amount),
            source_ref=row.source_ref,
            match_key=row.match_key,
        )
    return ledger.entries
