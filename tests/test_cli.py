"""CLI 错误呈现测试。"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from xrecon.cli import app

HEADER = "marketplace,order_id,sku,fee_type,amount,period,posted_at\n"
BAD_BODY = (
    "DEMO-M,DEMO-1,DEMO-SKU-001,principal,abc,2026-01,2026-01-05\n"
)


def _write_bad_csv(tmp_path: Path) -> Path:
    path = tmp_path / "bad.csv"
    path.write_text(HEADER + BAD_BODY, encoding="utf-8")
    return path


def test_import_bad_amount_reports_single_line_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["import", str(_write_bad_csv(tmp_path))])

    assert result.exit_code == 2
    assert "bad.csv:2" in result.output
    assert "Traceback" not in result.output
    assert result.stdout == ""
    assert result.stderr == (
        "错误: bad.csv:2（第 2 行）: amount 解析失败: 'abc'\n"
    )


def test_reconcile_bad_amount_reports_single_line_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["reconcile", str(_write_bad_csv(tmp_path))])

    assert result.exit_code == 2
    assert "bad.csv:2" in result.output
    assert "Traceback" not in result.output
    assert result.stdout == ""
    assert result.stderr == (
        "错误: bad.csv:2（第 2 行）: amount 解析失败: 'abc'\n"
    )
