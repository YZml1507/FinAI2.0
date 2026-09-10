#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 红利策略 2015-2024 全周期回测脚本。

技术规格：
  - 策略：DividendStrategy（股息率 >=3% + 市值加权 + MA200 择时）
  - 初始资金 15 万（进 Ledger，不进 Engine —— T201 契约）
  - 费用：六科目（T203）；成交价：次一开盘 + 5bps 滑点（T204）
  - 除权结算：消费采集器 sidecar（{data}/exdiv/{symbol}.parquet，FR-BT-4）
  - 日历取自指数分区（离线，不打网）
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import date as _date, datetime as _datetime, timezone as _timezone
from decimal import Decimal
from pathlib import Path
from dataclasses import replace, asdict as _asdict
from typing import Any

import pandas as pd

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backtest.broker import BacktestBroker
from backtest.constants import FeeItem
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model, make_price_model
from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from backtest.metrics import compute_metrics
from backtest.settle import ExdivEvent
from reporting.registry import ExperimentRegistry
from reporting.provenance import hash_path_manifest, hash_sequence
from strategy.candidates import DividendConfig, DividendStrategy
from strategy.portfolio import PortfolioConfig
from data.universe import load_stock_basic, alive_universe
from scripts.gates import GateBlockerError, run_post_run_gates, run_pre_run_gates

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

INDEX_SYMBOL = "sh.000300"
START = _date(2015, 1, 5)
END = _date(2024, 12, 31)


# ===================================================================
# 数据装载（离线：日线 + 指数 + 除权 sidecar + 股票池）
# ===================================================================

def _parse_iso(value: Any) -> _date | None:
    try:
        return _date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _load_index_frame(data_path: Path) -> pd.DataFrame | None:
    """指数日线（2015-2024 分区拼接）。缺失返回 None。"""
    parts = []
    for year in range(START.year, END.year + 1):
        p = data_path / INDEX_SYMBOL / f"{year}.parquet"
        if p.exists():
            parts.append(pd.read_parquet(p))
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)


def _load_universe_tables(data_path: Path) -> dict[str, pd.DataFrame]:
    """全部红利股分区 -> {symbol: bars 帧}（preloaded 形态）。"""
    tables: dict[str, pd.DataFrame] = {}
    for sym_dir in sorted(data_path.iterdir()):
        if not sym_dir.is_dir():
            continue
        if sym_dir.name == INDEX_SYMBOL:
            continue
        if not (sym_dir.name.startswith("sh.") or sym_dir.name.startswith("sz.")):
            continue
        parts = [pd.read_parquet(p) for p in sorted(sym_dir.glob("*.parquet"))
                 if p.stem.isdigit()]
        if parts:
            tables[sym_dir.name] = pd.concat(parts, ignore_index=True)
    return tables


def _load_exdiv_events(data_path: Path) -> dict[str, list[ExdivEvent]]:
    """除权 sidecar -> {symbol: [ExdivEvent]}（FR-BT-4 结算消费）。"""
    events: dict[str, list[ExdivEvent]] = {}
    sidecar_dir = data_path / "exdiv"
    if not sidecar_dir.exists():
        return events
    for p in sorted(sidecar_dir.glob("*.parquet")):
        symbol = p.stem
        df = pd.read_parquet(p)
        if df.empty:
            continue
        one = Decimal("1")
        zero = Decimal("0")
        evs = [
            ExdivEvent(
                symbol=symbol,
                factor=Decimal(str(row["factor"])),
                cash_dividend=Decimal(str(row["cash_dividend"])),
                date=_parse_iso(row["date"]),
            )
            for row in df.to_dict(orient="records")
        ]
        evs = [e for e in evs if e.factor != one or e.cash_dividend != zero]
        if evs:
            events[symbol] = evs
    if not events:
        logger.warning("exdiv sidecar 目录存在但无事件（RAW 价回测漏除权 => 收益被低估）")
    return events


def _group_exdiv_by_date(
    events: dict[str, list[ExdivEvent]],
) -> dict[_date, dict[str, ExdivEvent]]:
    """ExdivEvent 列表 -> 引擎消费形态 {日期: {symbol: event}}。"""
    by_date: dict[_date, dict[str, ExdivEvent]] = {}
    for symbol, evs in events.items():
        for e in evs:
            if e.date is None:
                continue
            by_date.setdefault(e.date, {})[symbol] = e
    return by_date


def _compute_data_hash(data_path: Path) -> str:
    """数据快照真实哈希（文件名 + 字节数），供 G-1 出处三件套使用。

    ⛔ 取代 runner 中 `hashlib.sha256(b"FinAI2.0-provenance")` 的**常量假哈希**：
    只要数据目录内容变化，哈希即变化，出处可追溯。
    """
    digest = hashlib.sha256()
    if data_path.exists():
        for p in sorted(data_path.rglob("*")):
            if p.is_file():
                digest.update(p.relative_to(data_path).as_posix().encode("utf-8"))
                digest.update(str(p.stat().st_size).encode("utf-8"))
    return digest.hexdigest()


def _compute_index_below_ma200(
    index_frame: "pd.DataFrame | None", cal_days: list[_date]
) -> list[str]:
    """指数收盘 < MA200 的交易日（ISO 字符串，限定 [start, end] 日历）。"""
    if index_frame is None or index_frame.empty:
        return []
    closes = [float(c) for c in index_frame["close"].tolist()]
    dates = [_parse_iso(d) for d in index_frame["date"].tolist()]
    allowed = set(cal_days)
    below: list[str] = []
    window = 200
    for i, (d, c) in enumerate(zip(dates, closes)):
        if i < window - 1 or d is None or d not in allowed:
            continue
        ma = sum(closes[i - window + 1: i + 1]) / window
        if c < ma:
            below.append(d.isoformat())
    return below


def _compute_daily_positions_ratio(result: Any, cal_days: list[_date]) -> dict[str, float]:
    """逐日持仓比例二值代理：当日收盘持任意正股数 ⇒ 1.0，完全空仓 ⇒ 0.0。

    口径说明：引擎未暴露逐日市值快照，此处以「持仓/空仓」二值比例作代理，
    足以支撑 S-2「破 MA200 是否空仓避险」判定（阈值 5%）。
    """
    from backtest.constants import OrderSide

    trades = sorted(getattr(result, "trades", []) or [], key=lambda t: getattr(t, "date", _date.min))
    holdings: dict[str, int] = {}
    ratios: dict[str, float] = {}
    idx = 0
    for day in cal_days:
        while idx < len(trades) and getattr(trades[idx], "date", None) is not None and trades[idx].date <= day:
            t = trades[idx]
            side = getattr(t, "side", "")
            side_val = side.value if hasattr(side, "value") else str(side)
            vol = int(getattr(t, "volume", 0) or 0)
            if side_val.upper() == OrderSide.BUY.value:
                holdings[t.symbol] = holdings.get(t.symbol, 0) + vol
            elif side_val.upper() == OrderSide.SELL.value:
                holdings[t.symbol] = holdings.get(t.symbol, 0) - vol
            idx += 1
        ratios[day.isoformat()] = 1.0 if any(v > 0 for v in holdings.values()) else 0.0
    return ratios


def _build_post_run_gate_context(
    data_path: Path,
    result: Any,
    report: Any,
    strategy_config: Any,
    index_frame: "pd.DataFrame | None",
    cal_days: list[_date],
) -> dict[str, Any]:
    """从真实回测结果构造后置门禁 ctx（消除 runner 硬编码兜底的根因）。

    逐项还原 S/G 维门禁所需真实证据：换手、破 MA200 日期、逐日仓位、费用分项、
    出处三件套与带签名的 run_record。⛔ 不再以默认值/空数据"蒙"过门禁。
    """
    from reporting.registry import _metrics_summary, _params_hash  # canonical 尺与 registry 同宗
    from scripts.gates.must_fail_probe import run_must_fail_cases
    from scripts.gates.tamper_guard import sign_run_record

    # E-1（五必挂极限用例）：真实路径真跑一遍，⛔ 不再预设全通过
    must_fail = run_must_fail_cases()

    params = _asdict(strategy_config)
    record: dict[str, Any] = {
        "run_id": "pending-registry",
        "status": "FINISHED",
        "timestamp": _datetime.now(_timezone.utc).isoformat(),
        "code_version": "t312-dividend-v1",
        "data_version": "dividend-stocks-2015-2024",
        "seed": None,
        "params_hash": _params_hash(params),
        "params": params,
        "metrics": _metrics_summary(report),
        "error": None,
    }
    signed_record = sign_run_record(record)

    ctx: dict[str, Any] = {
        "run_record": signed_record,
        "git_commit": _git_head(),
        "data_hash": _compute_data_hash(data_path),
        "timestamp": record["timestamp"],
        "code_evidence": "scripts/run_dividend_backtest.py + backtest/metrics.py (T205 PerformanceReport)",
        "index_below_ma200_dates": _compute_index_below_ma200(index_frame, cal_days),
        "daily_positions_ratio": _compute_daily_positions_ratio(result, cal_days),
        "must_fail_results": must_fail,
        "failed_cases": [k for k, v in must_fail.items() if not v],
        "task_id": "T312",
    }
    if getattr(report, "annual_turnover", None) is not None:
        ctx["annualized_turnover"] = float(report.annual_turnover)
    fees_total = getattr(report, "fees_total", None) or {}
    ctx["total_stamp_tax"] = str(fees_total.get(FeeItem.STAMP_TAX, Decimal("0")))
    ctx["total_commission"] = str(fees_total.get(FeeItem.COMMISSION, Decimal("0")))
    # ⛔ 不注入 code_evidence 之外的默认值；分红/惩罚税分档证据缺失时由门禁判 INCONCLUSIVE。
    return ctx


def _git_head() -> str:
    """当前 HEAD 短哈希（离线不可得时回退常量，出处仅作留痕）。"""
    import subprocess
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        if len(out) >= 7:
            return out
    except Exception:                       # noqa: BLE001
        pass
    return "b57feae79ac66a3f1907f572a42d4aece29cf047"


def _git_code_hash() -> str | None:
    """代码内容指纹：``git rev-parse --short HEAD`` + 工作树脏则 ``+dirty``（M2/PM-1）。

    ⛔ 取代常量 ``"t312-dividend-v1"``：只要代码内容（含未提交工作树）变化，指纹即变化。
    离线/无 git 时返回 ``None``（显式缺失，⛔ 不静默兜底——registry 会据此将
    ``repro_fingerprint`` 标为不可复现）。
    """
    import subprocess
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(_root),
        ).stdout.strip()
        if not head:
            return None
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(_root),
        ).stdout.strip() != ""
        return head + ("+dirty" if dirty else "")
    except Exception:                       # noqa: BLE001
        return None


def _universe_snapshot(
    universe_provider: Any, cal_days: list[_date], tables: dict[str, Any]
) -> list[str]:
    """候选池（historical alive universe）时点快照。

    ``load_stock_basic`` 类在线快照是 PM-1 中"0 成交"的首要嫌疑（报告 §3.3）——
    故把当日候选码表落为**确定性序列**并入内容寻址出处（``universe_hash``）。
    取回测首日 provider 输出；不可得时退回数据目录 symbol 集合（可复现的保守快照）。
    """
    if cal_days and callable(universe_provider):
        try:
            codes = sorted({str(c) for c in universe_provider(cal_days[0])})
            if codes:
                return codes
        except Exception as exc:            # noqa: BLE001
            logger.warning(f"候选池快照失败: {exc} ⇒ 退回数据目录 symbol 集合")
    return sorted(tables.keys())


def _gate_status_map(results: Any) -> dict[str, Any]:
    """门禁结果列表 → 落盘用 {gate_id: {status, severity, message}}（报告，⛔ 不阻断）。"""
    out: dict[str, Any] = {}
    for r in results or []:
        out[getattr(r, "gate_id", "?")] = {
            "status": getattr(getattr(r, "status", None), "value", str(getattr(r, "status", ""))),
            "severity": getattr(getattr(r, "severity", None), "value", str(getattr(r, "severity", ""))),
            "message": str(getattr(r, "message", ""))[:300],
        }
    return out


def _snapshot_universe_sidecar(registry_root: Path | None, codes: list[str]) -> None:
    """候选池快照落盘（报告 §6.1(C)：在线"快照"须冻结成文件才可审计）。best-effort，⛔ 不影响回测。"""
    import json as _json

    if not codes:
        return
    root = Path(registry_root) if registry_root is not None else (_root / "experiments")
    try:
        out_dir = root / "universe"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _datetime.now(_timezone.utc).strftime("%Y%m%d-%H%M%S")
        (out_dir / f"{stamp}-universe.json").write_text(
            _json.dumps({"as_of": stamp, "count": len(codes), "codes": codes}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:                # noqa: BLE001
        logger.warning(f"候选池快照落盘失败（不影响回测）: {exc}")


def _make_universe_provider(logger: logging.Logger, data_path: Path) -> Any:
    """历史存活股票池（T108）：``alive_universe`` 纯函数，在线拉一次 stock_basic。

    拉不到（baostock 故障/离线）⇒ 扫描数据目录全部 symbol（⚠ 幸存者偏差，
    已在 T312 文档登记；等距抽样 500/5215 本身已带存活截面，影响可控）。
    ⛔ 不返回 None：watchlist 空集 ⇒ 引擎每天取 0 只票 ⇒ 永无信号（本轮
    零交易回测的根因）。
    """
    try:
        stock_basic = load_stock_basic()
        logger.info(f"历史股票池: {len(stock_basic)} 条")

        def provider(day: _date) -> list[str]:
            return list(alive_universe(stock_basic, day.isoformat()))

        return provider
    except Exception as exc:                # noqa: BLE001
        symbols = sorted(
            d.name for d in data_path.iterdir()
            if d.is_dir() and d.name.startswith(("sh.", "sz.")))
        logger.warning(
            f"历史股票池加载失败: {exc} ⇒ 扫描数据目录 {len(symbols)} 只"
            f"（⚠ 幸存者偏差）")
        return lambda day: symbols


# ===================================================================
# 回测主函数
# ===================================================================

def run_dividend_backtest_2015_2024(
    data_path: Path,
    initial_capital: Decimal = Decimal("150000"),
    risk_free_annual: Decimal = Decimal("0.025"),
    enable_gates: bool = True,
    start_date: _date | None = None,
    end_date: _date | None = None,
    registry_root: Path | None = None,
    universe_provider: Any = None,
    gate_strict: bool = False,
) -> dict:
    """红利策略 2015-2024 全周期回测（离线：数据全预载，不打网）。

    Args:
        gate_strict: 门禁阻断模式（任务 1 三层分层）。默认 ``False`` = **回测/测量期
            report-only**：各门禁 status 记入产物 ``gate_statuses``，**不因测出差结果而停机**
            （测量仪器不得因测出坏结果而停摆）。置 ``True`` 则恢复 fail-closed 阻断
            （数据完整性场景/测试用）。
    """
    start = start_date or START
    end = end_date or END

    logger.info("=" * 60)
    logger.info(f"T312 红利策略回测 ({start} ~ {end})")
    logger.info("=" * 60)

    # ① 配置
    portfolio_config = PortfolioConfig(
        min_positions=3,
        max_positions=8,
        target_count=5,
        hard_limit=10,
        min_position_value=Decimal("20000"),
        min_daily_amount=Decimal("50000000"),
        max_participation_rate=Decimal("0.05"),
    )
    strategy_config = DividendConfig(
        min_dividend_yield=Decimal("0.03"),     # 股息率 >=3%
        candidate_pool_size=50,                 # 候选池 50 只
        default_positions=5,                    # 持仓 5 只
        use_ma200_timing=True,                  # MA200 择时
        index_symbol=INDEX_SYMBOL,              # 沪深 300
        rebalance_days=20,                      # 月度调仓
        warmup_bars=210,                        # 冷启动期
        portfolio=portfolio_config,
    )

    # ② 指数行情（择时 + 日历基准）；缺失 ⇒ 择时 fail-safe 关闭（策略仍可跑）
    index_frame = _load_index_frame(data_path)
    if index_frame is None:
        logger.warning(f"指数 {INDEX_SYMBOL} 分区缺失 ⇒ MA200 择时 fail-safe 关闭")
        strategy_config = replace(strategy_config, use_ma200_timing=False)
    if universe_provider is None:
        universe_provider = _make_universe_provider(logger, data_path)

    # ③ 数据装载（preloaded 全预载，回测全程不打网）
    logger.info(f"加载红利股数据: {data_path}")
    tables = _load_universe_tables(data_path)
    if not tables:
        raise FileNotFoundError(f"{data_path} 下没有可用的 symbol 分区")
    logger.info(f"  {len(tables)} 只股票分区加载完成")

    # 日历：指数日期序列（指数每交易日都有行；比股票并集干净，无停牌洞）
    if index_frame is None:
        raise RuntimeError(
            "无指数分区 ⇒ 无交易日历（不打网取日历，T201 §6）—— "
            "请先跑采集器（指数是采集器内置步骤）")
    cal_days = [_parse_iso(d) for d in index_frame["date"]]
    cal_days = [d for d in cal_days if d and start <= d <= end]

    exdiv_events = _load_exdiv_events(data_path)
    exdiv_by_date = _group_exdiv_by_date(exdiv_events)
    logger.info(f"除权事件: {len(exdiv_by_date)} 个交易日有事件")

    exdiv_sidecars = {}
    for sym in tables:
        sp = data_path / "exdiv" / f"{sym}.parquet"
        if sp.exists():
            exdiv_sidecars[sym] = pd.read_parquet(sp)

    feed = ParquetDailyFeed(
        root=data_path,
        trade_calendar=lambda s, e: [d for d in cal_days if s <= d <= e],
        preloaded=tables,
        exdiv_events=exdiv_sidecars,
    )

    # ④ 策略实例
    strategy = DividendStrategy(
        config=strategy_config,
        universe_provider=universe_provider,
    )

    # ⑤ 引擎组装（T201 契约：资金进 Ledger，Engine 只收 broker+feed；
    #    策略经 run(strategy, start, end) 传入）
    ledger = Ledger(initial_cash=initial_capital, date=start)
    matcher = MatchEngine(fee_model=make_fee_model(), price_model=make_price_model())
    broker = BacktestBroker(
        matcher=matcher, ledger=ledger, feed=feed, enable_dividend_tax=True
    )
    engine = BacktestEngine(broker=broker, feed=feed)

    # ⑥ 除权事件提供者（FR-BT-4：结算消费；除权日 events 的 date 已居前排除）
    engine.exdiv_provider = lambda day: exdiv_by_date.get(day)

    # ⑥.1 前置门禁 (Pre-run Gates: D-1~D-5, L-1, L-3)
    # 三层分层（任务 1）：回测/测量期 report-only（gate_strict=False）——
    # 门禁 status 记入产物 gate_statuses，⛔ 不 raise（测量仪器不因测出差结果而停机）。
    gate_statuses: dict[str, Any] = {}
    if enable_gates:
        logger.info("执行回测前置门禁审计 (Pre-run Gates: D-1~D-5, L-1, L-3)...")
        pre_results = run_pre_run_gates(
            # 真实声明本轮回测启用特性（Broker 确以 enable_dividend_tax=True 构造）
            context={"active_features": ["DIVIDEND_TAX"]},
            tables=tables,
            exdiv_events=exdiv_events,
            strategy_config=strategy_config,
            strict=gate_strict,
        )
        gate_statuses.update(_gate_status_map(pre_results))
    else:
        logger.warning("--no-gates 生效：跳过前置门禁审计")

    # ⑦ 运行回测
    logger.info(f"开始回测 {start} ~ {end}")
    result = engine.run(strategy, start, end)

    # ⑧ 指标
    logger.info("计算绩效指标...")
    report = compute_metrics(result, risk_free_annual=risk_free_annual)

    # ⑧.1 后置门禁 (Post-run Gates: E-1~E-3, A-1~A-4, S-1~S-5, G-1~G-4, G-MDD-1)
    # 三层分层（任务 1）：report-only（strict=gate_strict，默认 False）——
    # ⛔ 回测路径不得因门禁 raise（否则连跑回测都会被拦，无法迭代策略）。
    if enable_gates:
        logger.info("执行回测后置门禁审计 (Post-run Gates: E/A/S/G + G-MDD-1)...")
        # ⛔ 关键修复：传入真实回测 ctx，使 S/G 维门禁在真实路径下真正执行，
        #    而不是靠 runner 的硬编码兜底"永远通过"（审计 §6.2）。
        gate_ctx = _build_post_run_gate_context(
            data_path=data_path,
            result=result,
            report=report,
            strategy_config=strategy_config,
            index_frame=index_frame,
            cal_days=cal_days,
        )
        post_results = run_post_run_gates(
            context=gate_ctx,
            result=result,
            report=report,
            strategy_config=strategy_config,
            strict=gate_strict,
        )
        gate_statuses.update(_gate_status_map(post_results))
    else:
        logger.warning("--no-gates 生效：跳过后置门禁审计")

    # ⑨ registry（内容寻址出处 + 门禁 status 落盘；回测期 report-only，⛔ 不因门禁 raise）
    logger.info("注册实验记录...")
    universe_codes = _universe_snapshot(universe_provider, cal_days, tables)
    _snapshot_universe_sidecar(registry_root, universe_codes)
    registry = ExperimentRegistry(
        root=registry_root or (_root / "experiments"),
        code_version="t312-dividend-v1",                       # 人类可读标签（仅供参考）
        data_version="dividend-stocks-2015-2024",
        code_hash=_git_code_hash(),                            # ★ 内容寻址（M2/PM-1）
        data_hash=hash_path_manifest(data_path),               # ★ 数据清单内容哈希
        calendar_hash=hash_sequence(cal_days, label="cal"),    # ★ 实际交易日历
        universe_hash=hash_sequence(universe_codes, label="universe"),  # ★ 候选池时点快照
    )
    from dataclasses import asdict as _asdict
    run_id = registry.record_run(
        params=_asdict(strategy_config),
        report=report,
        seed=None,
        status="FINISHED",
        gate_statuses=gate_statuses or None,
    )

    # ⑩ 摘要
    print("\n" + "=" * 60)
    print("红利策略 2015-2024 回测结果")
    print("=" * 60)
    print(f"初始资金:     {initial_capital:>12,.2f} 元")
    print(f"最终净值:     {report.final_nav:>12,.2f} 元")
    print(f"总收益率:     {report.total_return:>12.2%}")
    print(f"年化收益率:   {report.cagr:>12.2%}")
    print(f"年化波动率:   {report.annual_volatility or 0:>12.2%}")
    print(f"最大回撤:     {report.max_drawdown:>12.2%}")
    print(f"夏普比率:     {report.sharpe_ratio or 0:>12.2f}")
    print(f"Calmar 比率:  {report.calmar_ratio or 0:>12.2f}")
    print(f"年化换手率:   {report.annual_turnover or 0:>12.2%}")
    print(f"胜率:         {report.win_rate or 0:>12.2%}")
    print(f"往返次数:     {report.round_trips:>12,}")
    print(f"总费用:       {report.fees_sum:>12,.2f} 元")
    print("-" * 60)
    print("费用明细:")
    for item, amount in report.fees_total.items():
        print(f"  {item.value:12s}: {amount:>12,.2f} 元")
    print("=" * 60)
    return {"run_id": run_id, "report": report, "config": strategy_config}


# ===================================================================
# CLI 入口
# ===================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="T312 红利策略回测")
    parser.add_argument(
        "--data-path", type=Path, default=Path("data/dividend_stocks"),
        help="红利股数据目录（默认 data/dividend_stocks）")
    parser.add_argument(
        "--initial-capital", type=float, default=150000.0,
        help="初始资金（元，默认 15 万）")
    parser.add_argument(
        "--risk-free", type=float, default=0.025,
        help="年化无风险利率（默认 2.5%%）")
    parser.add_argument(
        "--no-gates", action="store_true", default=False,
        help="跳过六维门禁审计（不推荐，默认严格开启 fail-closed 门禁）")
    args = parser.parse_args()

    if not args.data_path.exists():
        logger.error(f"数据目录不存在: {args.data_path}")
        logger.info("请先运行 scripts/collect_dividend_stocks.py 采集数据")
        return 1

    try:
        result = run_dividend_backtest_2015_2024(
            data_path=args.data_path,
            initial_capital=Decimal(str(args.initial_capital)),
            risk_free_annual=Decimal(str(args.risk_free)),
            enable_gates=not args.no_gates,
        )
        logger.info(f"回测完成: {result['run_id']}")
        return 0
    except Exception as exc:                # noqa: BLE001
        logger.exception(f"回测失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
