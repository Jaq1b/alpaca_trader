"""Command line: status, report, backtest, montecarlo, and the trading loop."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from tradingbot.backtest import Backtester
from tradingbot.bot import TradingBot
from tradingbot.broker import AlpacaBroker
from tradingbot.config import load_config
from tradingbot.display import (
    PositionView,
    format_account,
    format_positions,
    format_recent,
)
from tradingbot.env import load_env
from tradingbot.market_hours import MarketHours
from tradingbot.memory import TradeMemory
from tradingbot.metrics import (
    PerformanceAnalyzer,
    format_report,
    format_summary,
    report_to_json,
)
from tradingbot.montecarlo import simulate


class _LogFmt(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{ts}  {record.levelname}  {msg}"
        return f"{ts}  {msg}"


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_LogFmt())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


logger = logging.getLogger(__name__)


def _require_keys() -> tuple[str, str] | None:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        logger.error("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        return None
    return api_key, secret_key


def build_broker(config: dict) -> AlpacaBroker | None:
    keys = _require_keys()
    if keys is None:
        return None
    api_key, secret_key = keys
    return AlpacaBroker(
        api_key,
        secret_key,
        paper_trading=config.get("paper_trading", True),
        cache_config=config.get("cache"),
    )


def display_symbol(raw: str, crypto_symbols: list[str]) -> str:
    key = AlpacaBroker.normalize_symbol(raw)
    for symbol in crypto_symbols:
        if AlpacaBroker.normalize_symbol(symbol) == key:
            return symbol
    return str(raw).upper()


def _session_label(broker: AlpacaBroker) -> str:
    if MarketHours(broker).is_stock_market_open():
        return "regular hours"
    return "equities closed, crypto open"


def _position_views(
    positions: list[dict], crypto_symbols: list[str]
) -> list[PositionView]:
    rows: list[PositionView] = []
    for pos in positions:
        try:
            qty = float(pos.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0 or AlpacaBroker.is_dust_position(pos):
            continue
        symbol = display_symbol(str(pos.get("symbol") or ""), crypto_symbols)
        asset_class = "crypto" if "/" in symbol else "stock"
        entry = float(pos.get("avg_entry_price") or 0)
        last = float(pos.get("current_price") or 0)
        pnl = float(pos.get("unrealized_pl") or 0)
        pct_raw = pos.get("unrealized_plpc")
        pnl_pct = float(pct_raw) * 100 if pct_raw not in (None, "") else 0.0
        rows.append(
            PositionView(
                symbol=symbol,
                side="short" if qty < 0 else "long",
                qty=qty,
                entry=entry,
                last=last,
                pnl=pnl,
                pnl_pct=pnl_pct,
                asset_class=asset_class,
            )
        )
    return rows


def _recent_rows(
    memory: TradeMemory, limit: int = 8
) -> list[tuple[str, str, str, float, str]]:
    def stamp(trade) -> str:
        return trade.exit_timestamp or trade.timestamp or ""

    closed = sorted(memory.get_closed_trades(days=None), key=stamp)
    rows = []
    for trade in reversed(closed[-limit:]):
        when = stamp(trade)[:16].replace("T", " ")
        reason = trade.exit_reason or ""
        for prefix in ("BUY: ", "SHORT: ", "SELL: ", "COVER: "):
            if reason.startswith(prefix):
                reason = reason[len(prefix) :]
                break
        rows.append(
            (
                when,
                trade.symbol,
                trade.side or "long",
                float(trade.pnl or 0),
                reason,
            )
        )
    return rows


def cmd_status(config: dict) -> int:
    broker = build_broker(config)
    if broker is None:
        return 1
    account = broker.get_account(force=True)
    if not account or "account_number" not in account:
        logger.error("Could not reach Alpaca")
        return 1

    crypto = [str(s) for s in config.get("symbols", {}).get("crypto", [])]
    positions = broker.get_positions(force=True) or []
    views = _position_views(positions, crypto)
    equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    cash = float(account.get("cash") or 0)
    mode = "paper" if config.get("paper_trading", True) else "live"
    memory = TradeMemory(config.get("data_dir", "trading_data"))

    print()
    print(
        format_account(
            mode=mode,
            equity=equity,
            cash=cash,
            status=str(account.get("status") or "UNKNOWN"),
            session=_session_label(broker),
            open_count=len(views),
        )
    )
    print()
    print("Positions")
    print(format_positions(views))
    print()
    print("Recent closes")
    print(format_recent(_recent_rows(memory)))
    print()
    return 0


def cmd_report(config: dict, args: argparse.Namespace) -> int:
    data_dir = config.get("data_dir", "trading_data")
    memory = TradeMemory(data_dir)
    broker = None
    if not args.no_alpaca:
        broker = build_broker(config)
        if broker is None:
            logger.warning("No Alpaca keys; using the local ledger only")

    analyzer = PerformanceAnalyzer(
        memory,
        broker=broker,
        initial_capital=config.get("initial_capital"),
        portfolio_history_period=args.period,
    )
    report = analyzer.build_report()
    print()
    print(format_report(report) if args.verbose else format_summary(report))
    print()

    out_path = Path(args.json_out or Path(data_dir) / "metrics_snapshot.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_to_json(report))
    print(f"Saved {out_path}")
    return 0


def cmd_run(config: dict) -> int:
    broker = build_broker(config)
    if broker is None:
        return 1
    account = broker.get_account(force=True)
    if not account or "account_number" not in account:
        logger.error("Could not reach Alpaca")
        return 1
    status = str(account.get("status") or "")
    if status != "ACTIVE":
        logger.error(
            f"Account status is {status}. Orders need an ACTIVE paper account."
        )
        return 1
    try:
        TradingBot(broker, config=config).run()
    except KeyboardInterrupt:
        logger.info("Stopped")
    return 0


def _execute_backtest(config: dict, args: argparse.Namespace):
    """Run one replay. Returns the result, or None when the run cannot start."""
    started = time.perf_counter()
    broker = build_broker(config)
    if broker is None:
        return None
    try:
        tester = Backtester(
            broker,
            config,
            start=getattr(args, "start", None),
            end=getattr(args, "end", None),
        )
    except ValueError as exc:
        logger.error(str(exc))
        return None
    result = tester.run()
    result.elapsed_seconds = time.perf_counter() - started
    return result


def _print_recent(trades: list[dict]) -> None:
    if not trades:
        return
    print()
    print("Recent trades")
    for trade in trades[-8:]:
        r = trade.get("r_multiple")
        r_txt = f"{r:.2f}" if isinstance(r, (int, float)) else "n/a"
        print(
            f"  {trade['symbol']:<10} ${trade['pnl']:+,.2f}  "
            f"({trade['pnl_pct']:+.1f}%)  R {r_txt}  {trade.get('exit_reason')}"
        )
    print()


def cmd_backtest(config: dict, args: argparse.Namespace) -> int:
    result = _execute_backtest(config, args)
    if result is None:
        return 1
    print()
    print(result.summary())
    _print_recent(result.trades)
    return 0


def cmd_montecarlo(config: dict, args: argparse.Namespace) -> int:
    result = _execute_backtest(config, args)
    if result is None:
        return 1
    print()
    print(result.summary())
    if not result.trades:
        logger.error("Monte Carlo needs at least one closed trade")
        return 1
    try:
        cloud = simulate(
            [trade["pnl"] for trade in result.trades],
            result.initial_capital,
            n_paths=args.paths,
            seed=args.seed,
        )
    except ValueError as exc:
        logger.error(str(exc))
        return 1
    print()
    print(cloud.summary())
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=(
            "Paper-trading bot for US stocks and crypto. "
            "With no command, this scans and sends orders (same as run)."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Scan and send orders until Ctrl+C")
    sub.add_parser(
        "status",
        help="Print equity, open positions, and recent closes. Does not trade.",
    )

    report = sub.add_parser(
        "report",
        help="Print closed-trade results and write a JSON snapshot. Does not trade.",
    )
    report.add_argument(
        "--json-out",
        default=None,
        help="JSON path (default: trading_data/metrics_snapshot.json)",
    )
    report.add_argument(
        "--no-alpaca",
        action="store_true",
        help="Use the local ledger only",
    )
    report.add_argument(
        "--period",
        default="3M",
        help="Alpaca portfolio-history window, such as 1M or 3M",
    )
    report.add_argument(
        "--verbose",
        action="store_true",
        help="Include Sharpe, Sortino, and drawdown",
    )

    backtest = sub.add_parser(
        "backtest",
        help="Replay historical bars. Does not send orders.",
    )
    backtest.add_argument(
        "--start",
        default=None,
        help="First day to open trades, YYYY-MM-DD",
    )
    backtest.add_argument(
        "--end",
        default=None,
        help="Last day to open trades, YYYY-MM-DD. Default is today.",
    )

    montecarlo = sub.add_parser(
        "montecarlo",
        help="Replay once, then resample those trades. Does not send orders.",
    )
    montecarlo.add_argument(
        "--start",
        default=None,
        help="First day to open trades, YYYY-MM-DD",
    )
    montecarlo.add_argument(
        "--end",
        default=None,
        help="Last day to open trades, YYYY-MM-DD. Default is today.",
    )
    montecarlo.add_argument(
        "--paths",
        type=int,
        default=2000,
        help="How many resampled equity curves to build. Default 2000.",
    )
    montecarlo.add_argument(
        "--seed",
        type=int,
        default=1,
        help="Random seed. The same seed repeats the same paths.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    configure_logging()
    # Quiet third-party noise if any library logs at INFO.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    config = load_config()

    if command == "status":
        return cmd_status(config)
    if command == "report":
        return cmd_report(config, args)
    if command == "backtest":
        return cmd_backtest(config, args)
    if command == "montecarlo":
        return cmd_montecarlo(config, args)
    return cmd_run(config)


if __name__ == "__main__":
    raise SystemExit(main())
