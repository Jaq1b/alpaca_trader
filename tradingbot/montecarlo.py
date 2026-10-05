"""Bootstrap the closed trades from one backtest.

Each path draws the same number of trades, with replacement, and rebuilds an
equity curve. The cloud shows how much of the result is the order of these
trades. It does not invent new prices or a new strategy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MonteCarloResult:
    initial_capital: float
    observed_ending: float
    n_trades: int
    n_paths: int
    seed: int
    ending_p5: float
    ending_p50: float
    ending_p95: float
    max_drawdown_p5: float
    max_drawdown_p50: float
    max_drawdown_p95: float
    probability_loss: float

    def summary(self) -> str:
        return "\n".join(
            [
                "Monte Carlo",
                f"  paths       {self.n_paths}  seed {self.seed}",
                f"  trades      {self.n_trades} resampled with replacement",
                f"  observed    ${self.observed_ending:,.2f}",
                f"  ending      p5 ${self.ending_p5:,.2f}  "
                f"p50 ${self.ending_p50:,.2f}  p95 ${self.ending_p95:,.2f}",
                f"  max DD      p5 {self.max_drawdown_p5:.1%}  "
                f"p50 {self.max_drawdown_p50:.1%}  "
                f"p95 {self.max_drawdown_p95:.1%}",
                f"  P(loss)     {self.probability_loss:.1%}",
                "  note        Draws with replacement treat these trades as independent.",
            ]
        )


def path_ending_and_drawdown(
    pnls: np.ndarray, initial_capital: float
) -> tuple[float, float]:
    """Ending equity and max drawdown for one sequence of trade P&Ls."""
    curve = np.empty(pnls.shape[0] + 1, dtype=float)
    curve[0] = initial_capital
    curve[1:] = initial_capital + np.cumsum(pnls)
    peaks = np.maximum.accumulate(curve)
    drawdown = np.where(peaks > 0, (peaks - curve) / peaks, 0.0)
    return float(curve[-1]), float(drawdown.max())


def simulate(
    pnls: list[float] | np.ndarray,
    initial_capital: float,
    n_paths: int = 2000,
    seed: int = 1,
) -> MonteCarloResult:
    """Resample trade P&L into `n_paths` equity curves."""
    series = np.asarray(list(pnls), dtype=float)
    series = series[np.isfinite(series)]
    if series.size == 0:
        raise ValueError("Monte Carlo needs at least one closed trade")
    if n_paths < 1:
        raise ValueError("Monte Carlo needs at least one path")
    if initial_capital <= 0:
        raise ValueError("Monte Carlo starting capital must be positive")

    rng = np.random.default_rng(seed)
    draws = rng.integers(0, series.size, size=(n_paths, series.size))
    sampled = series[draws]
    curve = np.empty((n_paths, series.size + 1), dtype=float)
    curve[:, 0] = initial_capital
    curve[:, 1:] = initial_capital + np.cumsum(sampled, axis=1)
    peaks = np.maximum.accumulate(curve, axis=1)
    drawdown = np.where(peaks > 0, (peaks - curve) / peaks, 0.0)
    ending = curve[:, -1]
    max_dd = drawdown.max(axis=1)
    return MonteCarloResult(
        initial_capital=float(initial_capital),
        observed_ending=float(initial_capital + series.sum()),
        n_trades=int(series.size),
        n_paths=int(n_paths),
        seed=int(seed),
        ending_p5=float(np.percentile(ending, 5)),
        ending_p50=float(np.percentile(ending, 50)),
        ending_p95=float(np.percentile(ending, 95)),
        max_drawdown_p5=float(np.percentile(max_dd, 5)),
        max_drawdown_p50=float(np.percentile(max_dd, 50)),
        max_drawdown_p95=float(np.percentile(max_dd, 95)),
        probability_loss=float(np.mean(ending < initial_capital)),
    )
