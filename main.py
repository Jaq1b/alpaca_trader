"""Entry point for paper/live trading and optional backtest runs."""

import logging
import os

from tradingbot.backtest import Backtester
from tradingbot.bot import TradingBot
from tradingbot.broker import AlpacaBroker
from tradingbot.config import load_config
from tradingbot.env import load_env

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _require_keys() -> tuple[str, str] | None:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        logger.error("Missing API keys! Set ALPACA_API_KEY and ALPACA_SECRET_KEY")
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
        logger.info("Initializing trading bot...")
        broker = _build_broker(config)
        if broker is None:
            return

        account = broker.get_account(force=True)
        if not account or "account_number" not in account:
            logger.error("Failed to connect to Alpaca API")
            return

        bot = TradingBot(broker, config=config)
        bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")


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
        print(
            f"  {trade['symbol']}: ${trade['pnl']:.2f} "
            f"({trade['pnl_pct']:.1f}%) R={trade.get('r_multiple')} | "
            f"{trade.get('exit_reason')}"
        )


if __name__ == "__main__":
    main()
