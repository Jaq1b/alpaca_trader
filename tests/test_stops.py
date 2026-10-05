"""R-based stop updates and the quiet-range entry filter."""

from tradingbot.sizing import tighten_stop
from tradingbot.strategy import SignalStrategy


class _Memory:
    def get_recent_performance(self, symbol, days=1):
        return []


def test_breakeven_after_one_r():
    # Entry 100, initial stop 99, so 1R is 101.
    stop = tighten_stop(
        side="long",
        entry_price=100,
        stop_loss=99,
        price=101,
        risk_per_unit=1,
    )
    assert stop == 100


def test_trail_locks_half_an_r_after_one_and_a_half():
    # +1.5R at 101.5, trail 1R behind → stop at 100.5.
    stop = tighten_stop(
        side="long",
        entry_price=100,
        stop_loss=99,
        price=101.5,
        risk_per_unit=1,
    )
    assert stop == 100.5


def test_stop_does_not_loosen():
    stop = tighten_stop(
        side="long",
        entry_price=100,
        stop_loss=100.8,
        price=101.5,
        risk_per_unit=1,
    )
    assert stop == 100.8


def test_short_trail_moves_down():
    stop = tighten_stop(
        side="short",
        entry_price=100,
        stop_loss=101,
        price=98.5,
        risk_per_unit=1,
    )
    assert stop == 99.5


def test_quiet_range_is_skipped():
    strategy = SignalStrategy(
        _Memory(), {"atr_stop_mult": 2.5, "min_stop_pct_stock": 0.008}
    )
    # 2.5 * 0.20 = 0.50, which is inside 0.8% of 100.
    assert strategy._range_wide_enough(100, 0.20, "stock") is False
    # 2.5 * 0.40 = 1.00, which clears 0.8%.
    assert strategy._range_wide_enough(100, 0.40, "stock") is True
