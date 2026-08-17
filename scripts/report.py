"""Regenerate the current performance metrics snapshot.

Usage:
  python scripts/report.py
  python scripts/report.py --json-out trading_data/metrics_snapshot.json
  python scripts/report.py --no-alpaca   # local ledger only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow running as `python scripts/report.py` from repo root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingbot.broker import AlpacaBroker
from tradingbot.config import load_config
from tradingbot.env import load_env
from tradingbot.memory import TradeMemory
from tradingbot.metrics import PerformanceAnalyzer, format_report, report_to_json

load_env(ROOT)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("report")


def main():
    parser = argparse.ArgumentParser(description="Trading bot performance report")
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional path to write the structured JSON snapshot",
    )
    parser.add_argument(
        "--no-alpaca",
        action="store_true",
        help="Skip Alpaca API (ledger-only metrics)",
    )
    parser.add_argument(
        "--period",
        default="3M",
        help="Alpaca portfolio history period (default 3M)",
    )
    args = parser.parse_args()

    config = load_config()
    data_dir = config.get("data_dir", "trading_data")
    memory = TradeMemory(data_dir)

    broker = None
    if not args.no_alpaca:
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            logger.warning(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY not set — running ledger-only. "
                "Pass nothing extra once keys are exported, or use --no-alpaca."
            )
        else:
            broker = AlpacaBroker(
                api_key,
                secret_key,
                paper_trading=config.get("paper_trading", True),
                cache_config=config.get("cache"),
            )

    analyzer = PerformanceAnalyzer(
        memory,
        broker=broker,
        initial_capital=config.get("initial_capital"),
        portfolio_history_period=args.period,
    )
    report = analyzer.build_report()
    print(format_report(report))

    out_path = args.json_out
    if out_path is None:
        out_path = str(Path(data_dir) / "metrics_snapshot.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(report_to_json(report))
    print(f"\nWrote JSON snapshot -> {out_path}")
    return report


if __name__ == "__main__":
    main()
