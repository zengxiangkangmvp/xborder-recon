# xborder-recon

把平台结算 CSV 和手工记的账放到一起，回答一个问题：

> 平台扣的钱，和我记的账，差在哪一条、为什么差？

v0.1 是一个刻意保持小巧的命令行工具：标准库 `csv` + `Decimal`，双分录账本，
四类可追溯差异，合成演示数据。没有 pandas / duckdb / numpy。

## 3 分钟跑起来

要求 Python 3.12。

```bash
cd xborder-recon
python3 -m venv .venv
. .venv/bin/activate
pip install -e . pytest
python3 -m pytest -q
xrecon demo
```

`xrecon demo` 会在临时目录生成 60 行**完全虚构**的结算数据，跑完整条链路，
然后打印 Markdown 报告。它故意制造 5 条差异，覆盖全部 4 种类型：

```text
- 金额差（AMOUNT_MISMATCH）：1 条
- 跨月归属差（TIMING_DIFF）：1 条
- 账本缺失（MISSING_IN_LEDGER）：2 条
- 账本多记（MISSING_IN_SETTLEMENT）：1 条
```

想保留演示文件：

```bash
xrecon demo --out ./demo_data
xrecon reconcile ./demo_data/demo_settlement.csv --ledger ./demo_data/demo_ledger.csv
```

## 命令

| 命令 | 作用 |
|---|---|
| `xrecon demo [--out DIR]` | 生成合成数据 → 导入 → 记账 → 对账 → 打印报告 |
| `xrecon import <csv> [--rules PATH]` | 导入结算 CSV，生成双分录，展示幂等和借贷平衡 |
| `xrecon reconcile <csv> [--rules PATH] [--json] [--ledger PATH]` | 对比结算与手工账，输出 Markdown 或 JSON |

`reconcile` 不传 `--ledger` 时，会用结算数据自身生成一份账本做自洽检查（通常无差异）；
和真实手工账对比时请传 `--ledger`。手工账 CSV 与结算 CSV 使用相同列。

## 输入格式

结算 CSV 需要以下列（UTF-8，首行表头）：

```csv
marketplace,order_id,sku,fee_type,amount,period,posted_at
DEMO-MARKETPLACE,DEMO-2026-0001,DEMO-SKU-001,principal,19.90,2026-01,2026-01-05
DEMO-MARKETPLACE,DEMO-2026-0002,DEMO-SKU-001,commission,-2.60,2026-01,2026-01-07
```

- `amount`：正数收入、负数支出，保留两位小数
- `period`：归属月份，`YYYY-MM`
- `posted_at`：入账日期，`YYYY-MM-DD`
- 每行会记录文件 sha256 前 12 位和原文件物理行号，报告中的证据可回溯到具体行
- 同一文件重复导入按 sha256 去重，第二次返回空列表

## 对账规则

匹配键是 `(marketplace, sku, order_id, fee_type)`。同一键的多行先求和，再比较：

| 类型 | 判据 |
|---|---|
| `AMOUNT_MISMATCH` | 金额差绝对值 > `tolerance.amount` |
| `TIMING_DIFF` | 金额在容差内，但 `period` 不同 |
| `MISSING_IN_LEDGER` | 匹配键只在结算侧出现 |
| `MISSING_IN_SETTLEMENT` | 匹配键只在账本侧出现 |

默认容差 `0.01`：差 0.01 不报，差 0.02 报。

## 口径文件

包内自带 `src/xrecon/rules.yaml`，也可以通过 `--rules` 指定：

```yaml
version: 1
fx:
  source: fixed
  rates:
    USD: 7.10
    EUR: 7.75
allocation:
  ad_cost:
    method: sku_revenue    # by_sku_revenue | equal | manual
    manual: {}             # 仅 method: manual 时使用，sku -> 权重
timing:
  return_attribution: order_period
tolerance:
  amount: "0.01"
```

`Rules.digest()` 是规范化 JSON 的 sha256 前 12 位，会写进每份报告。数据相同、
rules digest 相同，换个人跑结果也应相同。

广告费分摊到分后如果有余数，会加到权重最大的 SKU 上，保证合计严格等于总额；
某个 SKU 收入为 0 或全部为 0 时退化为均分。

## 双分录

- 正数结算：借 `platform_receivable`，贷 `revenue:{fee_type}`
- 负数结算：借 `expense:{fee_type}`，贷 `platform_receivable`
- 每条分录金额为正，业务正负号由借贷方向表达
- `Ledger.balance()` 返回借方合计减贷方合计，必须为 0；分录结构损坏会抛
  `LedgerImbalanceError`

## 开发

```bash
. .venv/bin/activate
python3 -m pytest -q          # 43 个测试
python3 -m xrecon demo        # 与 xrecon demo 等价
python3 scripts/make_demo_data.py --out ./demo_data
```

代码结构：

```text
src/xrecon/models.py       # Money / MatchKey / SettlementRow / LedgerEntry / Diff
src/xrecon/ingest.py       # CSV 导入 + sha256 幂等
src/xrecon/ledger.py       # 双分录账本
src/xrecon/rules.py        # rules.yaml、digest、广告费分摊
src/xrecon/reconcile.py    # 四类差异
src/xrecon/report.py       # Markdown / 文本报告
src/xrecon/cli.py          # typer CLI
scripts/make_demo_data.py  # 合成演示数据
```

## 边界

v0.1 只做单平台结算 CSV 与手工账的差异解释，不做多平台适配、用户系统、在线看板，
也不抓取任何平台数据。仓库内所有演示数据均由脚本合成，业务标识全部以 `DEMO-` 开头。

## 设计取舍

- **只做对账，不做全量财务系统。** 目标是回答"差额在哪一条"，不是取代 ERP。
- **金额一律 Decimal，量化到分，ROUND_HALF_UP。** 浮点会让差异判定在 0.01 边界抖动。
- **按 (match_key, period) 比较，而不是只按 key 聚合。** 否则同一条业务在两个月的金额互换会被聚合成相等，漏报跨月归属错误。
- **输入只读一次。** sha256 和解析内容取自同一份 bytes，避免摘要与入账内容不一致。
- **口径写进 rules.yaml 并计算 digest。** 同一份数据 + 同一个 digest，任何人跑出来的数必须一致。

## 局限

- 只支持 Amazon Settlement V2 风格的列结构，其他平台需要加适配层。
- 广告费分摊只实现按 SKU 收入比例，未做点击归因。
- 没有持久化：每次运行从 CSV 重建，适合核对而非日常记账。
- 汇率按固定表，未接实时牌价。
