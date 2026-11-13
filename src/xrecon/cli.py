"""xrecon 命令行入口。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any, NoReturn, Optional

import typer

from .ingest import ImportLedger, file_digest, load_settlement_csv
from .ledger import Ledger, from_settlement
from .models import DIFF_KINDS, Diff
from .reconcile import match
from .report import render_markdown
from .rules import Rules

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="跨境平台结算与手工账本对账工具（v0.1）。",
)


def _fail(message: str) -> NoReturn:
    """输出错误并以非零码退出，不依赖具体 click 版本的异常类型。"""

    typer.echo(f"错误：{message}", err=True)
    raise typer.Exit(code=1)


def _fail_value_error(exc: ValueError) -> NoReturn:
    """把数据解析错误呈现为一行 CLI 错误，退出码 2。"""

    typer.echo(f"错误: {exc}", err=True)
    raise typer.Exit(code=2)


def _load_rules(path: Optional[Path]) -> Rules:
    try:
        return Rules.load(path)
    except Exception as exc:  # pydantic / yaml / IO 错误统一转成 CLI 参数错误
        raise typer.BadParameter(f"无法加载规则文件：{exc}", param_hint="--rules") from exc


def _load_demo_module():
    """按源码仓库布局加载演示数据脚本，避免引入额外包结构。"""

    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "make_demo_data.py",
        Path.cwd() / "scripts" / "make_demo_data.py",
    ]
    for script_path in candidates:
        if not script_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("xrecon_make_demo_data", script_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    _fail(
        "找不到 scripts/make_demo_data.py；请在源码仓库中运行 demo，"
        "或使用 --out 配合独立生成的 CSV。"
    )


def _diff_to_dict(diff: Diff) -> dict[str, Any]:
    return {
        "kind": diff.kind,
        "match_key": {
            "marketplace": diff.match_key.marketplace,
            "sku": diff.match_key.sku,
            "order_id": diff.match_key.order_id,
            "fee_type": diff.match_key.fee_type,
        },
        "settlement_total": f"{diff.settlement_total:.2f}",
        "ledger_total": f"{diff.ledger_total:.2f}",
        "delta": f"{diff.delta:.2f}",
        "period": diff.period,
        "evidence": list(diff.evidence),
    }


def _build_report(settlement_path: Path, ledger_path: Path, rules: Rules) -> str:
    importer = ImportLedger()
    settlement_rows = importer.import_file(settlement_path)
    if not settlement_rows:
        # 理论上临时目录里不会重复；这里仍显式检查，避免静默输出空报告。
        _fail("结算文件没有可导入的数据行")

    ledger_rows = load_settlement_csv(ledger_path)
    settlement_entries = from_settlement(settlement_rows, rules)
    ledger_entries = from_settlement(ledger_rows, rules)

    # 两侧都必须借贷平衡；手记账故意有差异，但每条分录仍应自洽。
    Ledger(settlement_entries).balance()
    Ledger(ledger_entries).balance()

    diffs = match(settlement_rows, ledger_entries, rules)
    found_kinds = {diff.kind for diff in diffs}
    # demo 是验收路径：如果演示数据不再覆盖四类差异，应立刻暴露问题。
    if not set(DIFF_KINDS).issubset(found_kinds):
        _fail("演示数据未覆盖全部四类差异，实际为: " + ", ".join(sorted(found_kinds)))

    return render_markdown(
        diffs,
        rules_digest=rules.digest(),
        settlement_digest=settlement_rows[0].source_file,
    )


@app.command()
def demo(
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="保留演示数据的目录；不传则在临时目录生成并在结束后删除。",
    ),
) -> None:
    """生成合成数据，跑通导入 → 记账 → 对账 → 打印报告。"""

    if out is None:
        with tempfile.TemporaryDirectory(prefix="xrecon-demo-") as tmp_dir:
            settlement_path, ledger_path = _load_demo_module().generate_demo_data(
                Path(tmp_dir)
            )
            report = _build_report(settlement_path, ledger_path, Rules.load())
        typer.echo(report)
        return

    out = Path(out)
    settlement_path, ledger_path = _load_demo_module().generate_demo_data(out)
    report = _build_report(settlement_path, ledger_path, Rules.load())
    typer.echo(report)
    typer.echo(
        f"\n演示数据已保留：{settlement_path} / {ledger_path}",
        err=True,
    )


@app.command("import")
def import_command(
    csv_path: Path = typer.Argument(
        ...,
        metavar="CSV",
        exists=True,
        dir_okay=False,
        readable=True,
        help="平台结算 CSV。",
    ),
    rules: Optional[Path] = typer.Option(
        None,
        "--rules",
        exists=True,
        dir_okay=False,
        readable=True,
        help="自定义 rules.yaml；不传使用包内默认规则。",
    ),
) -> None:
    """导入结算 CSV 并生成双分录，展示文件级幂等与借贷平衡。"""

    try:
        rules_obj = _load_rules(rules)
        importer = ImportLedger()
        rows = importer.import_file(csv_path)
        duplicate_rows = importer.import_file(csv_path)
        entries = from_settlement(rows, rules_obj)
        ledger = Ledger(entries)
        balance = ledger.balance()
    except ValueError as exc:
        _fail_value_error(exc)

    typer.echo(f"文件: {csv_path}")
    typer.echo(f"sha256: {file_digest(csv_path)}")
    typer.echo(f"rules digest: {rules_obj.digest()}")
    typer.echo(f"新增结算行: {len(rows)}")
    typer.echo(f"重复导入返回行数: {len(duplicate_rows)}（幂等）")
    typer.echo(f"生成分录: {len(entries)}")
    typer.echo(f"借贷差额: {balance:.2f}")


@app.command()
def reconcile(
    csv_path: Path = typer.Argument(
        ...,
        metavar="CSV",
        exists=True,
        dir_okay=False,
        readable=True,
        help="平台结算 CSV。",
    ),
    rules: Optional[Path] = typer.Option(
        None,
        "--rules",
        exists=True,
        dir_okay=False,
        readable=True,
        help="自定义 rules.yaml；不传使用包内默认规则。",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
    ledger_path: Optional[Path] = typer.Option(
        None,
        "--ledger",
        exists=True,
        dir_okay=False,
        readable=True,
        help="手工账 CSV（与结算 CSV 同结构）；不传则用结算数据自身生成账本。",
    ),
) -> None:
    """对比结算 CSV 与手工账，输出四类差异报告。"""

    try:
        rules_obj = _load_rules(rules)
        settlement_rows = load_settlement_csv(csv_path)
        # 摘要取自解析时使用的同一份字节，避免再次读取时文件已变化。
        settlement_digest = (
            settlement_rows[0].source_file
            if settlement_rows
            else file_digest(csv_path)[:12]
        )
        if ledger_path is not None:
            ledger_rows = load_settlement_csv(ledger_path)
            ledger_entries = from_settlement(ledger_rows, rules_obj)
            ledger_digest = (
                ledger_rows[0].source_file
                if ledger_rows
                else file_digest(ledger_path)[:12]
            )
        else:
            ledger_entries = from_settlement(settlement_rows, rules_obj)
            ledger_digest = settlement_digest

        Ledger(ledger_entries).balance()
        diffs = match(settlement_rows, ledger_entries, rules_obj)
    except ValueError as exc:
        _fail_value_error(exc)

    if json_output:
        payload = {
            "rules_digest": rules_obj.digest(),
            "settlement_digest": settlement_digest,
            "ledger_digest": ledger_digest,
            "diff_count": len(diffs),
            "diffs": [_diff_to_dict(diff) for diff in diffs],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(
        render_markdown(
            diffs,
            rules_digest=rules_obj.digest(),
            settlement_digest=settlement_digest,
        )
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
