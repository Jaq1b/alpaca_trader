"""Alpaca paper/live trading bot (equities + crypto)."""

__all__ = [
    "AlpacaBroker",
    "Backtester",
    "PerformanceAnalyzer",
    "TradingBot",
    "load_config",
]


def __getattr__(name: str):
    if name == "TradingBot":
        from tradingbot.bot import TradingBot

        return TradingBot
    if name == "AlpacaBroker":
        from tradingbot.broker import AlpacaBroker

        return AlpacaBroker
    if name == "Backtester":
        from tradingbot.backtest import Backtester

        return Backtester
    if name == "PerformanceAnalyzer":
        from tradingbot.metrics import PerformanceAnalyzer

        return PerformanceAnalyzer
    if name == "load_config":
        from tradingbot.config import load_config

        return load_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
