"""Bootstrap equity curves from a fixed list of trade P&Ls."""

import numpy as np
import pytest

from tradingbot.montecarlo import path_ending_and_drawdown, simulate


def test_one_path_drawdown():
    ending, max_dd = path_ending_and_drawdown(np.array([10.0, -30.0, 20.0]), 100.0)
    assert ending == 100.0
    assert max_dd == pytest.approx(30.0 / 110.0)


def test_resample_is_reproducible_and_centered_on_the_trades():
    pnls = [10.0, -4.0, 6.0, -2.0]
    first = simulate(pnls, 1000.0, n_paths=4000, seed=7)
    second = simulate(pnls, 1000.0, n_paths=4000, seed=7)
    assert first == second
    assert first.n_trades == 4
    assert first.observed_ending == pytest.approx(1010.0)
    assert first.ending_p5 < first.ending_p50 < first.ending_p95
    assert first.max_drawdown_p5 <= first.max_drawdown_p50 <= first.max_drawdown_p95
    assert 0.0 < first.probability_loss < 1.0


def test_all_losses_finish_down():
    result = simulate([-1.0, -2.0, -3.0], 100.0, n_paths=200, seed=1)
    assert result.probability_loss == 1.0
    assert result.ending_p95 < 100.0


def test_empty_trade_list():
    with pytest.raises(ValueError, match="closed trade"):
        simulate([], 10_000.0)


def test_montecarlo_command_parses():
    from tradingbot.cli import build_parser

    args = build_parser().parse_args(
        ["montecarlo", "--start", "2026-01-02", "--paths", "10", "--seed", "3"]
    )
    assert args.command == "montecarlo"
    assert args.paths == 10
    assert args.seed == 3
    assert args.start == "2026-01-02"
