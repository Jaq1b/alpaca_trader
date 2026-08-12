"""Unit tests for core ratio math with known synthetic series."""

from __future__ import annotations

import math

import numpy as np

from tradingbot.metrics import (
    EQUITY_PERIODS_PER_YEAR,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    trade_stats,
)
from tradingbot.models import Trade


def test_sharpe_known_series():
    # Constant positive excess return, zero variance after demeaning of a flat series
    # Use a simple alternating-ish series with hand-checkable mean/std.
    returns = [0.01, 0.02, -0.005, 0.015, 0.0, 0.01, -0.01, 0.02]
    arr = np.asarray(returns, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    expected = (mean / std) * math.sqrt(EQUITY_PERIODS_PER_YEAR)

    got = sharpe_ratio(returns, EQUITY_PERIODS_PER_YEAR, risk_free_rate=0.0)
    assert got is not None
    assert abs(got - expected) < 1e-9


def test_sharpe_zero_vol_returns_none():
    assert sharpe_ratio([0.01, 0.01, 0.01], 252) is None


def test_sharpe_too_short_returns_none():
    assert sharpe_ratio([0.01], 252) is None


def test_sortino_ignores_upside_volatility():
    # Series A has same downside as B but more upside — Sortino should be higher for A
    # when means differ accordingly. Construct identical downside, extra upside on A.
    base = [0.0, -0.02, 0.01, -0.01, 0.005, -0.015, 0.02, 0.0]
    more_upside = [0.05 if r > 0 else r for r in base]

    s_base = sortino_ratio(base, 252, risk_free_rate=0.0)
    s_up = sortino_ratio(more_upside, 252, risk_free_rate=0.0)
    assert s_base is not None and s_up is not None
    assert s_up > s_base

    # Hand-check downside deviation path for base
    arr = np.asarray(base, dtype=float)
    downside = np.minimum(arr, 0.0)
    downside_dev = math.sqrt(float(np.mean(downside**2)))
    expected = (float(arr.mean()) / downside_dev) * math.sqrt(252)
    assert abs(s_base - expected) < 1e-9


def test_sortino_no_downside_returns_none():
    assert sortino_ratio([0.01, 0.02, 0.03], 252) is None


def test_max_drawdown_known_curve():
    # Peak 110 -> trough 80 = 80/110 - 1; recovers above peak at 120
    equity = [100, 110, 105, 80, 90, 100, 120]
    dd, duration = max_drawdown(equity)
    assert dd is not None and duration is not None
    assert abs(dd - (80 / 110 - 1.0)) < 1e-12
    # Peak at index 1 (110), recovers to >=110 at index 6 (120) => duration 5
    assert duration == 5


def test_max_drawdown_short_series():
    assert max_drawdown([100.0]) == (None, None)


def test_trade_stats_win_rate_and_profit_factor():
    trades = [
        Trade(
            symbol="AAPL",
            entry_price=100,
            quantity=1,
            timestamp="2026-01-01T10:00:00",
            stop_loss=95,
            asset_class="stock",
            status="closed",
            pnl=10,
            exit_timestamp="2026-01-02T10:00:00",
            fees=1,
        ),
        Trade(
            symbol="BTC/USD",
            entry_price=100,
            quantity=1,
            timestamp="2026-01-01T10:00:00",
            stop_loss=95,
            asset_class="crypto",
            status="closed",
            pnl=-5,
            exit_timestamp="2026-01-03T10:00:00",
            fees=0,
        ),
        Trade(
            symbol="JPM",
            entry_price=100,
            quantity=1,
            timestamp="2026-01-01T10:00:00",
            stop_loss=95,
            asset_class="stock",
            status="open",
            pnl=0,
        ),
    ]
    stats = trade_stats(trades)
    # Net: +9 and -5
    assert stats["total_trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert abs(stats["win_rate"] - 0.5) < 1e-12
    assert abs(stats["gross_profit"] - 9.0) < 1e-12
    assert abs(stats["gross_loss"] - 5.0) < 1e-12
    assert abs(stats["profit_factor"] - 9.0 / 5.0) < 1e-12
    assert stats["by_asset_class"]["equity"] == 1
    assert stats["by_asset_class"]["crypto"] == 1


def test_insufficient_data_flags_in_flagged_ratio_helper():
    from tradingbot.metrics import _flagged_ratio

    flagged = _flagged_ratio(1.23, sufficient=False, n_obs=5)
    assert flagged["value"] == 1.23
    assert flagged["reliable"] is False
    assert flagged["reason"]

    ok = _flagged_ratio(1.23, sufficient=True, n_obs=40)
    assert ok["reliable"] is True
    assert ok["reason"] is None
