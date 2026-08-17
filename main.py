"""Paper/live trading entrypoint."""

import logging
import os
import sys

from tradingbot.backtest import Backtester
from tradingbot.bot import TradingBot
from tradingbot.broker import AlpacaBroker
from tradingbot.config import load_config
from tradingbot.env import load_env

load_env()


class _LogFmt(logging.Formatter):
    """Clock + message. Warnings/errors keep a level tag; info stays quiet."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{ts}  {record.levelname}  {msg}"
        return f"{ts}  {msg}"


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_LogFmt())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger(__name__)


def _require_keys() -> tuple[str, str] | None:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        logger.error(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY — copy .env.example to .env"
        )
        return None
    return api_key, secret_key


def _build_broker(config: dict) -> AlpacaBroker | None:
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


def main() -> None:
    config = load_config()
    try:
        broker = _build_broker(config)
        if broker is None:
            return

        account = broker.get_account(force=True)
        if not account or "account_number" not in account:
            logger.error("Could not reach Alpaca — check keys and network")
            return

        status = str(account.get("status") or "")
        if status != "ACTIVE":
            logger.error(
                f"Alpaca account status is {status} (need ACTIVE). "
                "Reset or create a paper account, then update .env."
            )
            return

        TradingBot(broker, config=config).run()
    except KeyboardInterrupt:
        logger.info("Stopped")
    except Exception as e:
        logger.error(f"Fatal: {e}")


def run_backtest() -> None:
    config = load_config()
    broker = _build_broker(config)
    if broker is None:
        return

    result = Backtester(broker, config).run()
    print(result.summary())
    if not result.trades:
        return

    print("\nRecent trades:")
    for trade in result.trades[-10:]:
        r = trade.get("r_multiple")
        r_txt = f"{r:.2f}" if isinstance(r, (int, float)) else "n/a"
        print(
            f"  {trade['symbol']}: ${trade['pnl']:.2f} "
            f"({trade['pnl_pct']:.1f}%)  R={r_txt}  {trade.get('exit_reason')}"
        )


if __name__ == "__main__":
    main()
