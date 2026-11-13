"""口径规则：汇率、费用分摊、跨月归属与差异容忍度。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .models import CENT, Money, to_decimal

__all__ = [
    "AdCostRules",
    "AllocationRules",
    "FxRules",
    "Rules",
    "TimingRules",
    "ToleranceRules",
]

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.yaml")


class FxRules(BaseModel):
    source: Literal["fixed", "manual"] = "fixed"
    rates: dict[str, Decimal] = Field(default_factory=dict)


class AdCostRules(BaseModel):
    method: Literal["sku_revenue", "by_sku_revenue", "equal", "manual"] = "sku_revenue"
    manual: dict[str, Decimal] = Field(default_factory=dict)


class AllocationRules(BaseModel):
    ad_cost: AdCostRules = Field(default_factory=AdCostRules)


class TimingRules(BaseModel):
    return_attribution: Literal["order_period", "refund_period"] = "order_period"


class ToleranceRules(BaseModel):
    amount: Decimal = Decimal("0.01")


class Rules(BaseModel):
    """运行口径。同一份数据配同一个 rules digest，结果应完全可复现。"""

    version: int = 1
    fx: FxRules = Field(default_factory=FxRules)
    allocation: AllocationRules = Field(default_factory=AllocationRules)
    timing: TimingRules = Field(default_factory=TimingRules)
    tolerance: ToleranceRules = Field(default_factory=ToleranceRules)

    @classmethod
    def load(cls, path: Path | None = None) -> "Rules":
        """从 YAML 加载规则；不传路径时使用包内置的 ``rules.yaml``。"""

        rules_path = Path(path) if path is not None else DEFAULT_RULES_PATH
        with rules_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{rules_path.name}: rules 文件顶层必须是映射")
        return cls.model_validate(data)

    def digest(self) -> str:
        """规范化 JSON 的 sha256 前 12 位，用于运行记录。"""

        payload = self.model_dump(mode="json")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    def allocate_ad_cost(
        self,
        total: Decimal,
        revenue_by_sku: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        """把一笔广告费按规则分摊到 SKU，保证分摊合计等于总额。

        舍入到分后产生的余数全部加到权重最大的 SKU 上，因此不会出现
        ``99.99 != 100.00`` 这类尾差。
        """

        total_value = Money.parse(total).amount
        if not revenue_by_sku:
            return {}

        keys = sorted(revenue_by_sku)
        revenues = {key: to_decimal(revenue_by_sku[key]) for key in keys}
        method = self.allocation.ad_cost.method

        if method == "equal":
            weights = {key: Decimal("1") for key in keys}
        elif method == "manual":
            manual = self.allocation.ad_cost.manual
            weights = {key: to_decimal(manual.get(key, 0)) for key in keys}
            if sum(weights.values(), Decimal("0")) <= 0:
                # 手工权重为空或全零时退化为均分，避免整笔费用丢失。
                weights = {key: Decimal("1") for key in keys}
        else:
            # sku_revenue / by_sku_revenue 是同一算法的两种写法。
            # 某 SKU 收入为 0（或负数）或全部为 0 时，按需求退化为均分。
            if any(value <= 0 for value in revenues.values()):
                weights = {key: Decimal("1") for key in keys}
            else:
                weights = revenues

        weight_sum = sum(weights.values(), Decimal("0"))
        if weight_sum <= 0:
            weights = {key: Decimal("1") for key in keys}
            weight_sum = Decimal(len(keys))

        allocations: dict[str, Decimal] = {}
        with localcontext() as context:
            context.prec = 50
            for key in keys:
                share = total_value * weights[key] / weight_sum
                allocations[key] = share.quantize(CENT, rounding=ROUND_HALF_UP)

        remainder = total_value - sum(allocations.values(), Decimal("0.00"))
        if remainder != 0:
            # keys 已排序，max 在权重相同时会返回字典序最小的 SKU，结果稳定。
            target = max(keys, key=lambda key: weights[key])
            allocations[target] = (allocations[target] + remainder).quantize(CENT)

        return allocations
