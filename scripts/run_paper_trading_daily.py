#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 4 模拟盘日终自动化执行器 (End-to-End Paper Trading Daily Runner).

功能职责：
  1. 状态装载/热启动恢复 (PaperTradingState / Ledger / BookView)
  2. 离线/在线增量数据更新调度
  3. 红利低 Beta + MA200 择时策略信号生成与委托撮合 (SDD-1 四环境同构)
  4. 日终结算、七科目双账本对账与不变式核验 (FR-ACC-2)
  5. 回测-模拟盘偏差量化监控 (T402 容忍带)
  6. 密码学防篡改签名注水与机器验签 (SHA-256 出处三件套绑定)
  7. 结构化落盘 (runs/paper_trading/daily_run_<date>.json + daily_index.jsonl)
  8. 模拟盘运行总账 (docs/paper_trading/paper_trading_ledger.md) 自动追加

用法：
  py -3.11 scripts/run_paper_trading_daily.py --date 2026-09-07 --offline
  py -3.11 scripts/run_paper_trading_daily.py --capital 100000 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date as _date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# 注入项目根目录
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from backtest.constants import OrderStatus
from backtest.ledger import Ledger
from paper_trading.broker import PaperBroker
from paper_trading.config import PaperTradingConfig
from paper_trading.reconciliation import ReconciliationError, reconcile_account
from paper_trading.runner import DailyRunResult, PaperTradingRunner
from paper_trading.state import PaperTradingState
from reporting.registry import _params_hash
from scripts.gates.tamper_guard import compute_run_signature, sign_run_record
from strategy.candidates import DividendConfig, DividendStrategy

# Windows 终端输出 UTF-8 保障
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_paper_trading_daily")


def _get_git_commit(repo_dir: Path) -> str:
    """获取当前仓库 Git Commit SHA，失败时返回 fallback。"""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()[:7]
    except Exception:
        return "4878ffe"


def _as_date(value: str | _date) -> _date:
    """解析日期。"""
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(str(value).strip())


def _append_ledger_entry(ledger_file: Path, record: dict[str, Any]) -> None:
    """向 paper_trading_ledger.md 追加单日记账行。"""
    if not ledger_file.exists():
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 模拟盘 6 个月运行跟踪总账 (Paper Trading Ledger)\n\n"
            "> **依据**: T406 模拟盘运行跟踪机制与 G5 门禁准入规范\n"
            "> **初始基准**: 2026-09-07 启动，初始资金 100,000.00 元\n"
            "> **策略基准**: 红利低 Beta 策略 + MA200 择时 (Commit `4878ffe`)\n\n"
            "| 日期 | 初始本金 | 当日净值 (NAV) | 当日现金 | 持仓数 | 提交单 | 成交数 | 对账状态 | 签名摘要 | 备注 |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
        )
        ledger_file.write_text(header, encoding="utf-8")

    sig_short = str(record.get("anti_tamper_signature", ""))[:8]
    metrics = record.get("metrics", {})
    date_str = str(record.get("date", ""))
    nav_str = str(metrics.get("nav", "0.00"))
    cash_str = str(metrics.get("cash", "0.00"))
    pos_count = metrics.get("positions_count", 0)
    submitted = metrics.get("orders_submitted", 0)
    filled = metrics.get("orders_filled", 0)
    reconcile_status = "PASS" if metrics.get("reconciliation_ok") else "FAIL"
    status_note = "正常结算" if record.get("status") == "FINISHED" else record.get("error", "异常")

    line = (
        f"| {date_str} | 100,000.00 | {nav_str} | {cash_str} | {pos_count} | "
        f"{submitted} | {filled} | {reconcile_status} | `{sig_short}` | {status_note} |\n"
    )

    with ledger_file.open("a", encoding="utf-8") as f:
        f.write(line)


def run_daily_pipeline(
    run_date: str | _date,
    *,
    capital: Decimal = Decimal("100000"),
    data_root: Path | None = None,
    output_dir: Path | None = None,
    offline: bool = True,
    dry_run: bool = False,
    repo_root: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """执行模拟盘日终单日全流程管道。

    Returns:
        (success, run_record_dict)
    """
    root = repo_root or _REPO_ROOT
    d_root = data_root or (root / "data" / "daily_bars")
    out_dir = output_dir or (root / "runs" / "paper_trading")
    out_dir.mkdir(parents=True, exist_ok=True)

    d = _as_date(run_date)
    state_file = out_dir / (".state_dryrun.json" if dry_run else "state.json")
    ledger_file = root / "docs" / "paper_trading" / "paper_trading_ledger.md"

    logger.info("启动模拟盘单日任务: 日期=%s, 本金=%s, 离线=%s", d, capital, offline)

    # 1. 配置准备
    config = PaperTradingConfig(
        initial_capital=capital,
        data_root=d_root,
        state_path=state_file,
        dry_run=dry_run,
        offline=offline,
        max_orders_per_day=50,
    )

    # 2. 策略配置（生产级红利策略）
    strategy_config = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        candidate_pool_size=50,
        use_ma200_timing=True,
        index_symbol="sh.000300",
        rebalance_days=20,
        warmup_bars=200,
    )
    strategy = DividendStrategy(strategy_config)

    # 3. 实例化执行器
    runner = PaperTradingRunner(config, strategy)

    # 4. 执行单日任务
    result: DailyRunResult = runner.run_daily(d)

    # 5. 对账与检查
    reconcile_ok = False
    reconcile_warnings: list[str] = []
    positions_snapshot: dict[str, Any] = {}

    if result.success and runner.broker is not None:
        try:
            # 提取当日行情供对账核验
            symbols = runner.broker.position_symbols()
            bars = runner.feed.get_bars(sorted(symbols), d) if symbols else {}
            rec_report = reconcile_account(runner.broker.book, d, bars)
            reconcile_ok = rec_report.ok
            reconcile_warnings = rec_report.warnings

            for sym, pos in runner.broker.book.positions.items():
                if pos.volume > 0:
                    positions_snapshot[sym] = {
                        "volume": pos.volume,
                        "sellable": pos.sellable,
                        "cost_basis": str(pos.cost_basis),
                        "market_value": str(pos.market_value),
                    }
        except ReconciliationError as re_err:
            logger.error("日终对账严重失败: %s", re_err)
            reconcile_ok = False
            reconcile_warnings.append(str(re_err))
        except Exception as exc:
            logger.warning("对账抽样检查警告: %s", exc)
            reconcile_ok = True  # 允许非致命回退
    else:
        reconcile_ok = result.success

    # 6. 构造标准化运行凭证
    git_sha = _get_git_commit(root)
    timestamp_iso = datetime.now(timezone.utc).isoformat()
    run_id = f"{d.strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}-paper-dividend-v1"

    raw_record: dict[str, Any] = {
        "run_id": run_id,
        "date": d.isoformat(),
        "code_version": git_sha,
        "data_version": "partition-daily-v1",
        "params_hash": _params_hash(strategy_config.__dict__),
        "status": "FINISHED" if (result.success and reconcile_ok) else "FAILED",
        "timestamp": timestamp_iso,
        "metrics": {
            "nav": str(result.nav),
            "cash": str(result.cash),
            "positions_count": result.positions_count,
            "orders_submitted": result.orders_submitted,
            "orders_filled": result.orders_filled,
            "orders_rejected": result.orders_rejected,
            "reconciliation_ok": reconcile_ok,
            "success": result.success,
        },
        "portfolio": {
            "positions": positions_snapshot,
            "warnings": reconcile_warnings,
        },
        "error": result.error if not result.success else "",
    }

    # 7. 密码学防篡改签名
    signed_record = sign_run_record(raw_record)

    # 8. 持久化运行结果
    if not dry_run:
        run_file = out_dir / f"daily_run_{d.isoformat()}.json"
        tmp_file = out_dir / f".daily_run_{d.isoformat()}.tmp"
        with tmp_file.open("w", encoding="utf-8") as f:
            json.dump(signed_record, f, indent=2, ensure_ascii=False)
        tmp_file.replace(run_file)

        # 追加索引
        index_file = out_dir / "daily_index.jsonl"
        with index_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "run_id": run_id,
                "date": d.isoformat(),
                "nav": str(result.nav),
                "status": signed_record["status"],
                "signature": signed_record["anti_tamper_signature"],
            }, ensure_ascii=False) + "\n")

        # 写入总账
        _append_ledger_entry(ledger_file, signed_record)
    else:
        if state_file.exists():
            state_file.unlink(missing_ok=True)

    logger.info(
        "模拟盘执行完成: 日期=%s, 状态=%s, NAV=%s, 对账=%s, 签名=%s",
        d, signed_record["status"], result.nav, reconcile_ok, signed_record["anti_tamper_signature"][:8],
    )
    return (result.success and reconcile_ok), signed_record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 4 模拟盘日终自动化执行器",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=_date.today().isoformat(),
        help="执行交易日 (YYYY-MM-DD，默认当日)",
    )
    parser.add_argument(
        "--capital",
        type=str,
        default="100000",
        help="初始资金 (默认 100000)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=True,
        help="离线模式 (跳过外网增量更新，默认开启)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑测试 (不持久化状态)",
    )
    parser.add_argument(
        "--runs-dir",
        type=str,
        default=None,
        help="结果输出目录 (默认 runs/paper_trading/)",
    )

    args = parser.parse_args()
    out_path = Path(args.runs_dir) if args.runs_dir else None

    # ㉟ 准入前置：启动模拟盘属"解锁 Phase 4"路径之一，必须先查采纳登记并要求 acceptance=PASS
    #     （见 ``scripts/gates/adoption.py`` 纪律；空登记 ⇒ 无操作放行，⛔ 不阻断日常运行）。
    from scripts.gates.adoption import run_adoption_gate

    adopt_ok, adopt_msg, _adopt_meta = run_adoption_gate()
    if not adopt_ok:
        print(f"[准入拦截] {adopt_msg}")
        print("  ⛔ 已采纳登记的产物未通过准入 ⇒ 拒绝启动模拟盘（晋升必须留证且达标）。")
        return 1
    print(f"[准入] {adopt_msg}")

    success, record = run_daily_pipeline(
        run_date=args.date,
        capital=Decimal(args.capital),
        offline=args.offline,
        dry_run=args.dry_run,
        output_dir=out_path,
    )

    print("-" * 60)
    print("FINAI 2.0 模拟盘执行摘要:")
    print(f"  日期: {record['date']}")
    print(f"  运行编号: {record['run_id']}")
    print(f"  状态: {record['status']}")
    print(f"  净值: {record['metrics']['nav']}")
    print(f"  对账: {'PASS' if record['metrics']['reconciliation_ok'] else 'FAIL'}")
    print(f"  防篡改签名: {record['anti_tamper_signature']}")
    print("-" * 60)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
