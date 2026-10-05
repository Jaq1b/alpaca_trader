"""Precomputed replay snapshots match analyze_market on the trailing window."""

import numpy as np
import pandas as pd

from tradingbot.snapshots import TAIL_BARS, BarTable
from tradingbot.strategy import SignalStrategy


class _Memory:
    def get_recent_performance(self, symbol, days=1):
        return []


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    open_ = close + rng.normal(0, 0.05, n)
    high = np.maximum(open_, close) + rng.random(n) * 0.2
    low = np.minimum(open_, close) - rng.random(n) * 0.2
    volume = rng.integers(100, 5000, n).astype(float)
    index = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=index,
    )


def _assert_close(got, expected, path):
    if isinstance(expected, dict):
        assert set(got) == set(expected), path
        for key in expected:
            _assert_close(got[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, bool):
        assert got is expected or got == expected, f"{path}: {got!r} != {expected!r}"
        return
    if isinstance(expected, (int, float)):
        assert np.isclose(
            got, expected, rtol=0, atol=1e-9, equal_nan=True
        ), f"{path}: {got!r} != {expected!r}"
        return
    assert got == expected, path


def test_snapshot_matches_trailing_window():
    frame = _frame(400, seed=7)
    cfg = {"min_bars": 40, "rsi_period": 14, "atr_period": 14}
    table = BarTable(frame, cfg, "stock")
    strategy = SignalStrategy(_Memory(), cfg)
    for loc in range(39, len(frame)):
        window = frame.iloc[: loc + 1].tail(TAIL_BARS)
        expected = strategy.analyze_market(window, "TEST", "stock")
        got = table.snapshot(loc, "TEST")
        _assert_close(got, expected, f"loc {loc}")


def test_decisions_match_on_precomputed_snapshot():
    frame = _frame(180, seed=3)
    cfg = {"min_bars": 40, "rsi_oversold": 30, "rsi_overbought": 70}
    table = BarTable(frame, cfg, "stock")
    direct = SignalStrategy(_Memory(), cfg)
    cached = SignalStrategy(_Memory(), cfg)
    for loc in (40, 80, 119, 120, 150):
        window = frame.iloc[: loc + 1].tail(TAIL_BARS)
        snap = table.snapshot(loc, "TEST")
        assert direct.should_buy(window, "TEST", "stock") == cached.should_buy(
            None, "TEST", "stock", snapshot=snap
        )
        assert direct.should_short(window, "TEST", "stock") == cached.should_short(
            None, "TEST", "stock", snapshot=snap
        )
        assert direct.should_sell(window, "TEST", 100.0, "stock") == cached.should_sell(
            None, "TEST", 100.0, "stock", snapshot=snap
        )
        assert direct.should_abandon(
            window, "TEST", "stock", "long", -0.2
        ) == cached.should_abandon(None, "TEST", "stock", "long", -0.2, snapshot=snap)
