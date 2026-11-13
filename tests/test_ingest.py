"""CSV 导入与幂等测试。"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from xrecon.ingest import (
    ImportLedger,
    file_digest,
    from_bytes,
    load_settlement_csv,
)

HEADER = "marketplace,order_id,sku,fee_type,amount,period,posted_at\n"
D = Decimal


def _write(path: Path, body: str) -> Path:
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def test_file_digest_matches_hashlib(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "settlement.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,2026-01-05\n",
    )
    assert file_digest(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_load_fills_source_metadata_and_line_numbers(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "settlement.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,2026-01-05\n"
        "DEMO-MARKETPLACE,DEMO-ORDER-0002,DEMO-SKU-001,commission,-2.50,2026-01,2026-01-06\n",
    )
    rows = load_settlement_csv(path)
    digest = file_digest(path)
    assert len(rows) == 2
    assert rows[0].source_line == 2
    assert rows[1].source_line == 3
    assert rows[0].source_file == digest[:12]
    assert rows[0].source_ref == f"{digest[:12]}:2"
    assert rows[0].amount == D("10.00")
    assert rows[0].posted_at.isoformat() == "2026-01-05"


def test_amount_is_quantized_to_two_decimals(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "settlement.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,1.005,2026-01,2026-01-05\n",
    )
    assert load_settlement_csv(path)[0].amount == D("1.01")


def test_same_file_import_is_idempotent(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "settlement.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,2026-01-05\n",
    )
    importer = ImportLedger()
    first = importer.import_file(path)
    second = importer.import_file(path)
    assert len(first) == 1
    assert second == []
    assert importer.is_imported(file_digest(path))


def test_same_content_different_path_is_also_duplicate(tmp_path: Path) -> None:
    body = "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,2026-01-05\n"
    first_path = _write(tmp_path / "a.csv", body)
    second_path = _write(tmp_path / "b.csv", body)
    importer = ImportLedger()
    assert len(importer.import_file(first_path)) == 1
    assert importer.import_file(second_path) == []


def test_changed_file_is_imported_again(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "settlement.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,2026-01-05\n",
    )
    importer = ImportLedger()
    assert len(importer.import_file(path)) == 1
    _write(
        path,
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,11.00,2026-01,2026-01-05\n",
    )
    assert len(importer.import_file(path)) == 1
    assert len(importer.imported_digests) == 2


def test_amount_parse_error_contains_filename_and_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad_amount.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,2026-01-05\n"
        "DEMO-MARKETPLACE,DEMO-ORDER-0002,DEMO-SKU-001,commission,not-a-number,2026-01,2026-01-06\n",
    )
    with pytest.raises(ValueError, match=r"bad_amount\.csv:3.*amount"):
        load_settlement_csv(path)


def test_bad_posted_at_error_contains_filename_and_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad_date.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,10.00,2026-01,not-a-date\n",
    )
    with pytest.raises(ValueError, match=r"bad_date\.csv:2.*posted_at"):
        load_settlement_csv(path)


def test_missing_required_column_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "missing.csv"
    path.write_text(
        "marketplace,order_id,sku,fee_type,period,posted_at\n"
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,2026-01,2026-01-05\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="缺少列 amount"):
        load_settlement_csv(path)


def test_header_only_file_returns_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text(HEADER, encoding="utf-8")
    assert load_settlement_csv(path) == []


def test_import_failure_does_not_poison_digest(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "settlement.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,oops,2026-01,2026-01-05\n",
    )
    importer = ImportLedger()
    with pytest.raises(ValueError):
        importer.import_file(path)
    assert not importer.is_imported(file_digest(path))


@pytest.mark.parametrize("raw_amount", ["nan", "inf", "-inf"])
def test_non_finite_amount_is_rejected(
    tmp_path: Path,
    raw_amount: str,
) -> None:
    path = _write(
        tmp_path / "bad.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,"
        f"{raw_amount},2026-01,2026-01-05\n",
    )
    with pytest.raises(ValueError, match=r"bad\.csv:2.*非有限金额"):
        load_settlement_csv(path)


@pytest.mark.parametrize("period", ["", "2026-1", "2026-13"])
def test_invalid_period_is_rejected(tmp_path: Path, period: str) -> None:
    path = _write(
        tmp_path / "bad.csv",
        "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,"
        f"10.00,{period},2026-01-05\n",
    )
    with pytest.raises(ValueError, match=r"bad\.csv:2.*period 格式非法"):
        load_settlement_csv(path)


def test_from_bytes_uses_same_bytes_for_digest_and_rows() -> None:
    data = (
        HEADER
        + "DEMO-MARKETPLACE,DEMO-ORDER-0001,DEMO-SKU-001,principal,"
        "10.00,2026-01,2026-01-05\n"
    ).encode()
    rows = from_bytes(data, filename="memory.csv")
    assert rows[0].source_file == hashlib.sha256(data).hexdigest()[:12]
    assert rows[0].period == "2026-01"

