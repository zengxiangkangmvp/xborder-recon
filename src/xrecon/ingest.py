"""结算 CSV 导入与文件级幂等控制。"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import SettlementRow, quantize_money

__all__ = ["ImportLedger", "file_digest", "from_bytes", "load_settlement_csv"]

REQUIRED_COLUMNS = (
    "marketplace",
    "order_id",
    "sku",
    "fee_type",
    "amount",
    "period",
    "posted_at",
)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    """返回文件的完整 sha256（小写十六进制）。"""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_settlement_bytes(
    data: bytes,
    *,
    digest: str,
    filename: str,
) -> list[SettlementRow]:
    """解析已经读入内存的 CSV 字节，摘要与内容来自同一份 bytes。"""

    rows: list[SettlementRow] = []
    source_file = digest[:12]
    text = data.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
    if missing:
        raise ValueError(
            f"{filename}: CSV 缺少列 {', '.join(missing)}；"
            f"需要 {', '.join(REQUIRED_COLUMNS)}"
        )

    for record in reader:
        line_no = reader.line_num
        raw_amount = record.get("amount")
        try:
            amount = Decimal(str(raw_amount).strip())
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(
                f"{filename}:{line_no}（第 {line_no} 行）: amount 解析失败: {raw_amount!r}"
            ) from exc
        if not amount.is_finite():
            raise ValueError(
                f"{filename}:{line_no}（第 {line_no} 行）: "
                f"amount 解析失败: {raw_amount!r}（非有限金额）"
            )
        try:
            amount = quantize_money(amount)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(
                f"{filename}:{line_no}（第 {line_no} 行）: "
                f"amount 解析失败: {raw_amount!r}"
            ) from exc

        period = str(record.get("period") or "").strip()
        if not _PERIOD_RE.fullmatch(period):
            raise ValueError(
                f"{filename}:{line_no}（第 {line_no} 行）: "
                f"period 格式非法: {period!r}（应为 YYYY-MM）"
            )

        raw_posted_at = record.get("posted_at")
        try:
            posted_at = date.fromisoformat(str(raw_posted_at).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{filename}:{line_no}（第 {line_no} 行）: posted_at 解析失败: {raw_posted_at!r}"
            ) from exc

        rows.append(
            SettlementRow(
                marketplace=str(record.get("marketplace") or "").strip(),
                order_id=str(record.get("order_id") or "").strip(),
                sku=str(record.get("sku") or "").strip(),
                fee_type=str(record.get("fee_type") or "").strip(),
                amount=amount,
                period=period,
                posted_at=posted_at,
                source_file=source_file,
                source_line=line_no,
            )
        )

    return rows


def from_bytes(data: bytes, *, filename: str) -> list[SettlementRow]:
    """从内存中的文件字节解析结算 CSV。

    摘要直接由 ``data`` 计算，因此 ``source_file`` 与解析出的行必然一致，
    调用方也无需再次读取文件。
    """

    return _parse_settlement_bytes(
        data,
        digest=_digest_bytes(data),
        filename=filename,
    )


def load_settlement_csv(path: Path) -> list[SettlementRow]:
    """读取平台结算 CSV。

    ``source_line`` 记录原文件中的物理行号：表头是第 1 行，因此第一条数据
    是第 2 行。金额解析失败时抛出的 ``ValueError`` 会带上文件名与行号。
    整个文件只读取一次，digest 与解析内容来自同一份字节。
    """

    path = Path(path)
    return from_bytes(path.read_bytes(), filename=path.name)


class ImportLedger:
    """记录已经导入过的文件摘要，保证同一文件重复导入是幂等的。

    该类是进程内状态。v0.1 不持久化导入历史；CLI 每次运行都会新建实例。
    """

    def __init__(self) -> None:
        self._imported: set[str] = set()

    @property
    def imported_digests(self) -> tuple[str, ...]:
        return tuple(sorted(self._imported))

    @property
    def imported(self) -> frozenset[str]:
        """已导入摘要的只读快照。"""

        return frozenset(self._imported)

    def is_imported(self, digest: str) -> bool:
        return digest in self._imported

    def import_file(self, path: Path) -> list[SettlementRow]:
        """导入文件；同一 digest 第二次调用返回空列表。

        文件只读取一次：摘要与解析结果都来自同一份 bytes，避免两次读取
        之间文件被改写导致记录摘要与实际入账内容不一致。
        """

        path = Path(path)
        data = path.read_bytes()
        digest = _digest_bytes(data)
        if self.is_imported(digest):
            return []
        rows = _parse_settlement_bytes(
            data,
            digest=digest,
            filename=path.name,
        )
        # 解析成功后才登记，避免坏文件占用 digest。
        self._imported.add(digest)
        return rows

    def clear(self) -> None:
        self._imported.clear()
