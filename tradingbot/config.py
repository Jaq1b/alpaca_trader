"""Load `config.yaml` and apply environment overrides."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load knobs from config.yaml; env vars still override secrets & paper mode."""
    config_path = (
        Path(path) if path else Path(os.getenv("TRADING_CONFIG", DEFAULT_CONFIG_PATH))
    )

    if not config_path.exists():
        logger.warning(f"Config not found at {config_path}, using built-in defaults")
        return _defaults()

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    cfg = _defaults()
    _deep_update(cfg, data)

    if os.getenv("ALPACA_PAPER_TRADING") is not None:
        cfg["paper_trading"] = (
            os.getenv("ALPACA_PAPER_TRADING", "true").lower() == "true"
        )
    if os.getenv("TRADING_DATA_DIR"):
        cfg["data_dir"] = os.getenv("TRADING_DATA_DIR")

    return cfg


def _deep_update(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _defaults() -> dict[str, Any]:
    return {
        "paper_trading": True,
        "data_dir": "trading_data",
        "initial_capital": 1000.0,
        "symbols": {
            "stocks": "watchlist",
            "scan_batch_size": 100,
            "crypto": ["BTC/USD", "ETH/USD"],
        },
        "risk": {
            "crypto_per_trade": 0.012,
            "stock_per_trade": 0.012,
            "daily_crypto_limit": 2500.0,
            "daily_stock_limit": 5000.0,
            "max_positions": 6,
            "min_hold_seconds": 180,
            "check_interval_seconds": 45,
            "max_position_value_crypto": 200.0,
            "max_position_value_stock": 300.0,
            "min_position_value": 15.0,
            "allow_shorting": True,
        },
        "strategy": {
            "signal_cooldown": 300,
            "buy_score_min": 4,
            "sell_score_min": 3,
            "max_recent_losses": 3,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "atr_period": 14,
            "atr_stop_mult": 1.8,
            "atr_trail_mult": 2.0,
            "trail_arm_atr_mult": 1.5,
            "volume_mult_stock": 1.4,
            "volume_mult_crypto": 1.5,
            "take_profit_pct_stock": 4.0,
            "take_profit_pct_crypto": 6.0,
            "solid_profit_pct_stock": 2.5,
            "solid_profit_pct_crypto": 4.0,
            "min_bars": 40,
        },
        "cache": {
            "bars_ttl_seconds": 90,
            "price_ttl_seconds": 15,
            "account_ttl_seconds": 10,
            "positions_ttl_seconds": 10,
            "clock_ttl_seconds": 30,
            "calendar_ttl_seconds": 3600,
        },
        "backtest": {
            "lookback_days": 30,
            "initial_capital": 10000.0,
            "timeframe": "5Min",
            "bar_limit": 5000,
        },
    }
