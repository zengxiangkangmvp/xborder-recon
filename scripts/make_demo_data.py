"""生成完全虚构的对账演示数据。

结算文件约 60 行，覆盖 3 个 SKU、2 个月份。账本文件由结算数据派生，
并故意留下 4 类差异，方便第一次运行 ``xrecon demo`` 时看到效果。

所有业务标识均以 ``DEMO-`` 开头，不包含任何真实店铺、订单或金额。
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

MARKETPLACE = "DEMO-MARKETPLACE"
SKUS = ("DEMO-SKU-001", "DEMO-SKU-002", "DEMO-SKU-003")
PERIODS = ("2026-01", "2026-02")
FEE_TYPES = ("principal", "commission", "shipping")
COLUMNS = (
    "marketplace",
    "order_id",
    "sku",
    "fee_type",
    "amount",
    "period",
    "posted_at",
)

# 演示数据中故意制造的差异说明（供 README / 测试引用）。
DEMO_DEFECTS = {
    "amount_mismatch": 1,
    "timing_diff": 1,
    "missing_in_ledger": 2,
    "missing_in_settlement": 1,
}


def _amount_for(fee_type: str, sku_index: int, slot: int) -> Decimal:
    """用固定公式生成金额，保证每次运行完全一致。"""

    if fee_type == "principal":
        return Decimal("19.90") + Decimal(slot * 3) + Decimal(sku_index)
    if fee_type == "commission":
        return -(Decimal("2.50") + Decimal(slot) / Decimal("10"))
    return -(Decimal("1.10") + Decimal(sku_index) / Decimal("10"))


def _settlement_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    order_seq = 1
    for period in PERIODS:
        year, month = (int(part) for part in period.split("-"))
        for sku_index, sku in enumerate(SKUS):
            for slot in range(10):
                order_id = f"DEMO-2026-{order_seq:04d}"
                fee_type = FEE_TYPES[slot % len(FEE_TYPES)]
                amount = _amount_for(fee_type, sku_index, slot)
                rows.append(
                    {
                        "marketplace": MARKETPLACE,
                        "order_id": order_id,
                        "sku": sku,
                        "fee_type": fee_type,
                        "amount": f"{amount:.2f}",
                        "period": period,
                        "posted_at": date(year, month, 5 + slot * 2).isoformat(),
                    }
                )
                order_seq += 1
    return rows


def _ledger_rows(settlement_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从结算数据派生手记账，并注入四类差异。"""

    ledger_rows = [dict(row) for row in settlement_rows]

    # 1) 金额差 0.05：第 1 行账本多记 0.05。
    ledger_rows[0]["amount"] = f"{Decimal(ledger_rows[0]['amount']) + Decimal('0.05'):.2f}"

    # 2) 跨月：金额不变，只把账本期间从 2026-01 改成 2026-02。
    ledger_rows[1]["period"] = "2026-02"

    # 3) 账本缺失：结算有、账本无。删除第 3、4 行对应的两笔。
    del ledger_rows[2:4]

    # 4) 账本多记：结算无、账本有一笔虚构费用。
    ledger_rows.append(
        {
            "marketplace": MARKETPLACE,
            "order_id": "DEMO-2026-0901",
            "sku": "DEMO-SKU-001",
            "fee_type": "commission",
            "amount": "-4.20",
            "period": "2026-02",
            "posted_at": "2026-02-20",
        }
    )
    return ledger_rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def generate_demo_data(out_dir: Path) -> tuple[Path, Path]:
    """在 ``out_dir`` 下生成结算与手记账两个 CSV，返回其路径。"""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    settlement_rows = _settlement_rows()
    ledger_rows = _ledger_rows(settlement_rows)

    settlement_path = out_dir / "demo_settlement.csv"
    ledger_path = out_dir / "demo_ledger.csv"
    _write_csv(settlement_path, settlement_rows)
    _write_csv(ledger_path, ledger_rows)
    return settlement_path, ledger_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 xrecon 合成演示数据")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("demo_data"),
        help="输出目录（默认 ./demo_data）",
    )
    args = parser.parse_args(argv)
    settlement_path, ledger_path = generate_demo_data(args.out)
    print(f"已生成结算文件: {settlement_path}")
    print(f"已生成手记账文件: {ledger_path}")
    print(f"故意差异: {DEMO_DEFECTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
