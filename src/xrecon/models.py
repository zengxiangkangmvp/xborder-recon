"""核心数据模型。

v0.1 的所有金额一律使用 ``Decimal``，并在边界处量化为两位小数
（``ROUND_HALF_UP``）。禁止使用 ``float`` 参与金额计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

__all__ = [
    "CENT",
    "Diff",
    "DiffKind",
    "LedgerEntry",
    "MatchKey",
    "Money",
    "SettlementRow",
    "quantize_money",
    "to_decimal",
]

CENT = Decimal("0.01")

DiffKind = Literal[
    "AMOUNT_MISMATCH",
    "TIMING_DIFF",
    "MISSING_IN_LEDGER",
    "MISSING_IN_SETTLEMENT",
]

DIFF_KINDS: tuple[DiffKind, ...] = (
    "AMOUNT_MISMATCH",
    "TIMING_DIFF",
    "MISSING_IN_LEDGER",
    "MISSING_IN_SETTLEMENT",
)


def to_decimal(value: Any) -> Decimal:
    """把常见数值安全地转换成 ``Decimal``。

    ``float`` 先转成字符串再转换，避免把二进制浮点误差带进账本。
    """

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool 是 int 的子类，明确拒绝
        raise TypeError("金额不能是 bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value.strip())
    return Decimal(str(value))


def quantize_money(value: Any) -> Decimal:
    """量化为两位小数，使用财务常用的四舍五入（ROUND_HALF_UP）。"""

    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Money:
    """固定两位小数的金额值对象。

    构造时立即量化，因此 ``Money("1.005").amount == Decimal("1.01")``。
    """

    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", quantize_money(self.amount))

    # ---- 构造辅助 -----------------------------------------------------
    @classmethod
    def zero(cls) -> "Money":
        return cls(Decimal("0.00"))

    @classmethod
    def parse(cls, value: Any) -> "Money":
        return cls(to_decimal(value))

    from_value = parse

    @property
    def value(self) -> Decimal:
        """``amount`` 的别名，方便调用方读取。"""

        return self.amount

    def to_decimal(self) -> Decimal:
        return self.amount

    # ---- 运算 ---------------------------------------------------------
    @staticmethod
    def _coerce(other: Any) -> Decimal:
        if isinstance(other, Money):
            return other.amount
        return to_decimal(other)

    def __add__(self, other: Any) -> "Money":
        return Money(self.amount + self._coerce(other))

    def __radd__(self, other: Any) -> "Money":
        return Money(self._coerce(other) + self.amount)

    def __sub__(self, other: Any) -> "Money":
        return Money(self.amount - self._coerce(other))

    def __rsub__(self, other: Any) -> "Money":
        return Money(self._coerce(other) - self.amount)

    def __neg__(self) -> "Money":
        return Money(-self.amount)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount))

    def __mul__(self, other: Any) -> "Money":
        return Money(self.amount * self._coerce(other))

    def __truediv__(self, other: Any) -> "Money":
        return Money(self.amount / self._coerce(other))

    # ---- 比较与展示 ---------------------------------------------------
    def __lt__(self, other: Any) -> bool:
        return self.amount < self._coerce(other)

    def __le__(self, other: Any) -> bool:
        return self.amount <= self._coerce(other)

    def __gt__(self, other: Any) -> bool:
        return self.amount > self._coerce(other)

    def __ge__(self, other: Any) -> bool:
        return self.amount >= self._coerce(other)

    def __str__(self) -> str:
        return f"{self.amount:.2f}"

    def __format__(self, format_spec: str) -> str:
        if not format_spec:
            return str(self)
        return format(self.amount, format_spec)


@dataclass(frozen=True, slots=True, order=True)
class MatchKey:
    """对账匹配键：平台 + SKU + 订单 + 费用类型。"""

    marketplace: str
    sku: str
    order_id: str
    fee_type: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.marketplace, self.sku, self.order_id, self.fee_type)

    def display(self) -> str:
        return f"{self.marketplace}/{self.sku}/{self.order_id}/{self.fee_type}"


@dataclass(frozen=True, slots=True)
class SettlementRow:
    """平台结算报告中的一行。"""

    marketplace: str
    order_id: str
    sku: str
    fee_type: str
    amount: Decimal  # 正数收入，负数支出
    period: str  # YYYY-MM
    posted_at: date
    source_file: str  # sha256 前 12 位
    source_line: int  # 原文件中的物理行号（表头为第 1 行）

    @property
    def match_key(self) -> MatchKey:
        return MatchKey(
            marketplace=self.marketplace,
            sku=self.sku,
            order_id=self.order_id,
            fee_type=self.fee_type,
        )

    @property
    def source_ref(self) -> str:
        return f"{self.source_file}:{self.source_line}"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """双分录账本中的一条分录。"""

    entry_id: str
    period: str
    memo: str
    debit_account: str
    credit_account: str
    amount: Decimal
    source_ref: str  # 格式 sha256[:12]:line
    match_key: MatchKey

    def __post_init__(self) -> None:
        # 分录金额统一为两位小数；正负号由借贷方向表达。
        object.__setattr__(self, "amount", quantize_money(self.amount))

    @property
    def signed_amount(self) -> Decimal:
        """按账户方向还原业务金额，供对账聚合使用。

        ``from_settlement`` 产生的收入分录贷记 ``revenue:*``，支出分录借记
        ``expense:*``；账本金额本身始终为正。
        """

        if self.debit_account.startswith("expense:"):
            return -abs(self.amount)
        if self.credit_account.startswith("revenue:"):
            return abs(self.amount)
        return self.amount


@dataclass(frozen=True, slots=True)
class Diff:
    """一条对账差异。"""

    kind: DiffKind
    match_key: MatchKey
    settlement_total: Decimal
    ledger_total: Decimal
    delta: Decimal  # settlement_total - ledger_total
    evidence: list[str]
    period: str | None = None  # 差异归属月份；跨月配对时为结算侧月份

    def __post_init__(self) -> None:
        object.__setattr__(self, "settlement_total", quantize_money(self.settlement_total))
        object.__setattr__(self, "ledger_total", quantize_money(self.ledger_total))
        object.__setattr__(self, "delta", quantize_money(self.delta))

    @property
    def abs_delta(self) -> Decimal:
        return abs(self.delta)
