"""Date window for the bar replay."""

from datetime import date, datetime, timezone

import pytest

from tradingbot.backtest import (
    Backtester,
    BacktestResult,
    format_elapsed,
    in_window,
    parse_bound,
    resolve_window,
)


def test_date_range_includes_the_end_day():
    fetch_start, fetch_end, trade_start, trade_end = resolve_window(
        lookback_days=30,
        start="2026-01-02",
        end="2026-03-31",
        warmup_days=7,
    )
    assert trade_start == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert trade_end == datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert fetch_end == trade_end
    assert fetch_start == datetime(2025, 12, 26, tzinfo=timezone.utc)


def test_lookback_when_dates_omitted():
    now = datetime(2026, 10, 3, 15, tzinfo=timezone.utc)
    _, fetch_end, trade_start, trade_end = resolve_window(
        lookback_days=30, now=now, warmup_days=7
    )
    assert trade_end == now
    assert fetch_end == now
    assert trade_start == datetime(2026, 9, 3, 15, tzinfo=timezone.utc)


def test_end_only_uses_lookback():
    _, _, trade_start, trade_end = resolve_window(
        lookback_days=10, end="2026-03-31", warmup_days=7
    )
    assert trade_end == datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert trade_start == datetime(2026, 3, 21, 23, 59, 59, tzinfo=timezone.utc)


def test_yaml_date_end_includes_that_day():
    assert parse_bound(date(2026, 1, 2), is_end=True) == datetime(
        2026, 1, 2, 23, 59, 59, tzinfo=timezone.utc
    )


def test_start_after_end():
    with pytest.raises(ValueError, match="after end"):
        resolve_window(lookback_days=30, start="2026-04-01", end="2026-03-01")


def test_bad_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_bound("March", is_end=False)


def test_warmup_bars_are_outside_the_trade_window():
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert not in_window(datetime(2026, 1, 1, 20, tzinfo=timezone.utc), start, end)
    assert in_window(datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc), start, end)
    assert in_window(datetime(2026, 1, 31, 21, tzinfo=timezone.utc), start, end)
    assert not in_window(datetime(2026, 2, 1, tzinfo=timezone.utc), start, end)


def test_command_dates_override_config():
    tester = Backtester(
        broker=object(),
        config={
            "backtest": {
                "start": "2026-01-01",
                "end": "2026-01-31",
                "lookback_days": 30,
            },
            "strategy": {},
            "risk": {},
            "symbols": {"stocks": [], "crypto": []},
        },
        start="2026-06-01",
        end="2026-06-15",
    )
    assert tester.trade_start == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert tester.trade_end == datetime(2026, 6, 15, 23, 59, 59, tzinfo=timezone.utc)
    assert tester.window_label == "2026-06-01 to 2026-06-15"


def test_elapsed_format():
    assert format_elapsed(12.36) == "12.4s"
    assert format_elapsed(254) == "4m 14s"
    assert format_elapsed(3723) == "1h 2m 3s"


def test_summary_includes_elapsed():
    result = BacktestResult(
        initial_capital=10000,
        ending_capital=10000,
        total_pnl=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0,
        avg_pnl=0,
        avg_r=0,
        max_drawdown=0,
        trades_per_day=0,
        elapsed_seconds=254,
    )
    assert "elapsed     4m 14s" in result.summary()
