"""
Performance metrics from the SQLite ledger and Alpaca portfolio history.

Annualization: equity √252, crypto √365, blended √365. Risk-free rate: 0%.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.models import Trade

logger = logging.getLogger(__name__)

EQUITY_PERIODS_PER_YEAR = 252
CRYPTO_PERIODS_PER_YEAR = 365
BLENDED_PERIODS_PER_YEAR = 365
RISK_FREE_RATE = 0.0

# Don't report annualized ratios as if they're meaningful below this.
MIN_CALENDAR_DAYS = 30
MIN_RETURN_OBS = 21


def _normalize_asset_class(asset_class: str | None) -> str:
    raw = (asset_class or "").lower()
    if raw in {"stock", "stocks", "equity", "equities"}:
        return "equity"
    if raw in {"crypto", "cryptocurrency"}:
        return "crypto"
    return raw or "unknown"


def _parse_date(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        return pd.Timestamp(value).tz_localize(None).normalize()
    except Exception:
        try:
            return pd.Timestamp(str(value)[:10])
        except Exception:
            return None


def sharpe_ratio(
    returns: Sequence[float],
    periods_per_year: float,
    risk_free_rate: float = RISK_FREE_RATE,
) -> float | None:
    """Annualized Sharpe from a period return series. rf is annualized, default 0%."""
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 2:
        return None
    period_rf = risk_free_rate / periods_per_year
    excess = arr - period_rf
    std = float(np.std(excess, ddof=1))
    if std == 0.0:
        return None
    return float(np.mean(excess) / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: Sequence[float],
    periods_per_year: float,
    risk_free_rate: float = RISK_FREE_RATE,
    target: float = 0.0,
) -> float | None:
    """Annualized Sortino using downside deviation vs target (default 0)."""
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 2:
        return None
    period_rf = risk_free_rate / periods_per_year
    excess = arr - period_rf
    downside = np.minimum(arr - target, 0.0)
    downside_var = float(np.mean(downside**2))
    if downside_var == 0.0:
        return None
    downside_dev = math.sqrt(downside_var)
    return float(np.mean(excess) / downside_dev * math.sqrt(periods_per_year))


def max_drawdown(
    equity: Sequence[float],
) -> tuple[float | None, int | None]:
    """
    Max drawdown as a fraction (e.g. -0.12 = -12%) and duration in bars
    of the longest underwater spell that includes the max-DD trough.
    """
    arr = np.asarray(list(equity), dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 2:
        return None, None

    peak = -np.inf
    max_dd = 0.0
    peak_idx = 0
    dd_start = 0
    trough_idx = 0

    for i, value in enumerate(arr):
        if value > peak:
            peak = value
            peak_idx = i
        drawdown = (value / peak) - 1.0 if peak > 0 else 0.0
        if drawdown < max_dd:
            max_dd = drawdown
            dd_start = peak_idx
            trough_idx = i

    # Bars from the peak before the trough until equity recovers (or series end).
    recovery = len(arr) - 1
    peak_level = arr[dd_start]
    for j in range(trough_idx, len(arr)):
        if arr[j] >= peak_level:
            recovery = j
            break
    duration = int(recovery - dd_start)
    return float(max_dd), duration


def trade_stats(trades: Iterable[Trade]) -> dict[str, Any]:
    closed = [t for t in trades if (t.status or "").lower() == "closed"]
    pnls = [float(t.pnl or 0.0) - float(getattr(t, "fees", 0.0) or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flat = [p for p in pnls if p == 0]

    gross_profit = float(sum(wins)) if wins else 0.0
    gross_loss = float(abs(sum(losses))) if losses else 0.0
    avg_win = float(np.mean(wins)) if wins else None
    avg_loss = float(np.mean(losses)) if losses else None
    win_loss_ratio = (
        (avg_win / abs(avg_loss))
        if avg_win is not None and avg_loss not in (None, 0)
        else None
    )
    profit_factor = (
        (gross_profit / gross_loss)
        if gross_loss > 0
        else (None if gross_profit == 0 else float("inf"))
    )

    by_class: dict[str, int] = {"equity": 0, "crypto": 0, "unknown": 0}
    for t in closed:
        by_class[_normalize_asset_class(t.asset_class)] = (
            by_class.get(_normalize_asset_class(t.asset_class), 0) + 1
        )

    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flat),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "average_win": avg_win,
        "average_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "by_asset_class": by_class,
        "total_pnl": float(sum(pnls)) if pnls else 0.0,
    }


def sleeve_pnl(trades: Iterable[Trade]) -> dict[str, float]:
    totals = {"equity": 0.0, "crypto": 0.0, "combined": 0.0}
    for t in trades:
        if (t.status or "").lower() != "closed":
            continue
        net = float(t.pnl or 0.0) - float(getattr(t, "fees", 0.0) or 0.0)
        cls = _normalize_asset_class(t.asset_class)
        if cls in totals:
            totals[cls] += net
        totals["combined"] += net
    return totals


def daily_realized_pnl_series(
    trades: Sequence[Trade],
    asset_class: str | None = None,
) -> pd.Series:
    """Sum of realized net P&L by exit date, optionally filtered by sleeve."""
    rows = []
    want = _normalize_asset_class(asset_class) if asset_class else None
    for t in trades:
        if (t.status or "").lower() != "closed":
            continue
        cls = _normalize_asset_class(t.asset_class)
        if want and cls != want:
            continue
        day = _parse_date(t.exit_timestamp or t.timestamp)
        if day is None:
            continue
        net = float(t.pnl or 0.0) - float(getattr(t, "fees", 0.0) or 0.0)
        rows.append((day, net))

    if not rows:
        return pd.Series(dtype=float)

    df = (
        pd.DataFrame(rows, columns=["date", "pnl"])
        .groupby("date")["pnl"]
        .sum()
        .sort_index()
    )
    return df.astype(float)


def daily_returns_from_pnl(
    daily_pnl: pd.Series,
    capital: float,
    fill_calendar: bool,
    weekdays_only: bool,
) -> pd.Series:
    """Convert absolute daily P&L into simple returns vs a fixed capital base."""
    if daily_pnl.empty or capital <= 0:
        return pd.Series(dtype=float)

    start = daily_pnl.index.min()
    end = daily_pnl.index.max()
    if fill_calendar:
        idx = pd.date_range(start, end, freq="D")
        if weekdays_only:
            idx = idx[idx.weekday < 5]
        series = daily_pnl.reindex(idx, fill_value=0.0)
    else:
        series = daily_pnl
    return (series / capital).astype(float)


def portfolio_history_to_daily_returns(
    history: dict[str, Any],
) -> tuple[pd.Series, pd.Series]:
    """
    Build equity curve + daily returns from Alpaca portfolio history.

    Returns (equity_series, daily_returns).
    """
    timestamps = history.get("timestamp") or []
    equity = history.get("equity") or []
    if not timestamps or not equity or len(timestamps) != len(equity):
        return pd.Series(dtype=float), pd.Series(dtype=float)

    dates = [
        pd.Timestamp(datetime.utcfromtimestamp(int(ts))).normalize()
        for ts in timestamps
    ]
    eq = pd.Series(equity, index=pd.DatetimeIndex(dates), dtype=float)
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    # Collapse intraday to daily last if needed
    eq = eq.groupby(eq.index).last()
    rets = eq.pct_change().dropna()
    return eq, rets


def evaluation_coverage(
    trades: Sequence[Trade],
    equity_curve: pd.Series | None = None,
) -> dict[str, Any]:
    dates = []
    for t in trades:
        d = _parse_date(t.exit_timestamp or t.timestamp)
        if d is not None:
            dates.append(d)
    if equity_curve is not None and not equity_curve.empty:
        dates.extend(list(equity_curve.index))

    if not dates:
        return {
            "start": None,
            "end": None,
            "calendar_days": 0,
            "weeks_with_data": 0,
            "days_with_trade_exits": 0,
            "sufficient_for_ratios": False,
            "note": "No trade or equity history available.",
        }

    start = min(dates)
    end = max(dates)
    calendar_days = int((end - start).days) + 1
    exit_days = {
        _parse_date(t.exit_timestamp or t.timestamp)
        for t in trades
        if (t.status or "").lower() == "closed"
    }
    exit_days.discard(None)
    weeks = {d.to_period("W").start_time for d in dates}

    sufficient = calendar_days >= MIN_CALENDAR_DAYS
    note = None
    if not sufficient:
        note = (
            f"Evaluation window is only {calendar_days} calendar day(s) "
            f"(need >={MIN_CALENDAR_DAYS}). Annualized ratios are flagged, not reliable."
        )

    return {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "calendar_days": calendar_days,
        "weeks_with_data": len(weeks),
        "days_with_trade_exits": len(exit_days),
        "sufficient_for_ratios": sufficient,
        "note": note,
    }


def rolling_performance(
    daily_returns: pd.Series,
    daily_pnl: pd.Series,
) -> dict[str, Any]:
    if daily_returns.empty and daily_pnl.empty:
        return {"weekly": [], "monthly": []}

    pnl = daily_pnl if not daily_pnl.empty else daily_returns
    weekly = []
    monthly = []

    if not pnl.empty:
        w = pnl.groupby(pnl.index.to_period("W")).sum()
        for period, value in w.items():
            weekly.append(
                {
                    "period": str(period),
                    "pnl_or_return": float(value),
                }
            )
        m = pnl.groupby(pnl.index.to_period("M")).sum()
        for period, value in m.items():
            monthly.append(
                {
                    "period": str(period),
                    "pnl_or_return": float(value),
                }
            )

    return {"weekly": weekly, "monthly": monthly}


def _flagged_ratio(
    value: float | None,
    sufficient: bool,
    n_obs: int,
) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "reliable": False,
            "reason": "Insufficient return observations or zero volatility.",
        }
    if not sufficient or n_obs < MIN_RETURN_OBS:
        return {
            "value": float(value),
            "reliable": False,
            "reason": (
                f"Only {n_obs} return observations / evaluation window too short "
                f"(need >={MIN_RETURN_OBS} obs and >={MIN_CALENDAR_DAYS} days)."
            ),
        }
    return {"value": float(value), "reliable": True, "reason": None}


class PerformanceAnalyzer:
    """Builds a full metrics snapshot from TradeMemory + optional Alpaca broker."""

    def __init__(
        self,
        memory,
        broker=None,
        initial_capital: float | None = None,
        portfolio_history_period: str = "3M",
    ):
        self.memory = memory
        self.broker = broker
        self.portfolio_history_period = portfolio_history_period
        state = memory.get_bot_state() if memory else {}
        self.initial_capital = float(
            initial_capital
            if initial_capital is not None
            else state.get("initial_capital") or 100000.0
        )

    def build_report(self) -> dict[str, Any]:
        trades = self.memory.get_closed_trades(days=None)
        stats = trade_stats(trades)
        pnls = sleeve_pnl(trades)

        alpaca_account = {}
        alpaca_positions = []
        history: dict[str, Any] = {}
        if self.broker is not None:
            alpaca_account = self.broker.get_account(force=True) or {}
            alpaca_positions = self.broker.get_positions(force=True) or []
            history = (
                self.broker.get_portfolio_history(
                    period=self.portfolio_history_period, timeframe="1D", force=True
                )
                or {}
            )

        equity_curve, blended_returns = portfolio_history_to_daily_returns(history)
        coverage = evaluation_coverage(
            trades, equity_curve if not equity_curve.empty else None
        )

        capital = self.initial_capital
        if alpaca_account:
            # Prefer last equity from history base, else account portfolio value as sanity check only
            if history.get("base_value"):
                capital = float(history["base_value"])
            elif equity_curve is not None and not equity_curve.empty:
                capital = float(equity_curve.iloc[0])

        # Sleeve daily returns from realized trade P&L
        equity_pnl = daily_realized_pnl_series(trades, "equity")
        crypto_pnl = daily_realized_pnl_series(trades, "crypto")
        combined_pnl = daily_realized_pnl_series(trades, None)

        equity_rets = daily_returns_from_pnl(
            equity_pnl, capital, fill_calendar=True, weekdays_only=True
        )
        crypto_rets = daily_returns_from_pnl(
            crypto_pnl, capital, fill_calendar=True, weekdays_only=False
        )

        # Blended: prefer Alpaca MTM; fall back to realized combined calendar returns
        if blended_returns.empty:
            blended_returns = daily_returns_from_pnl(
                combined_pnl, capital, fill_calendar=True, weekdays_only=False
            )
            blended_source = "realized_trade_pnl"
        else:
            blended_source = "alpaca_portfolio_history"

        if equity_curve.empty and not combined_pnl.empty:
            # Synthetic equity curve from capital + cumulative realized P&L
            cum = combined_pnl.sort_index().cumsum()
            idx = pd.date_range(cum.index.min(), cum.index.max(), freq="D")
            equity_curve = (
                capital + cum.reindex(idx, method="ffill").fillna(0.0)
            ).astype(float)

        max_dd, max_dd_bars = max_drawdown(
            equity_curve.tolist() if not equity_curve.empty else []
        )
        sufficient = bool(coverage.get("sufficient_for_ratios"))

        equity_sharpe = sharpe_ratio(equity_rets.tolist(), EQUITY_PERIODS_PER_YEAR)
        crypto_sharpe = sharpe_ratio(crypto_rets.tolist(), CRYPTO_PERIODS_PER_YEAR)
        blended_sharpe = sharpe_ratio(
            blended_returns.tolist(), BLENDED_PERIODS_PER_YEAR
        )

        equity_sortino = sortino_ratio(equity_rets.tolist(), EQUITY_PERIODS_PER_YEAR)
        crypto_sortino = sortino_ratio(crypto_rets.tolist(), CRYPTO_PERIODS_PER_YEAR)
        blended_sortino = sortino_ratio(
            blended_returns.tolist(), BLENDED_PERIODS_PER_YEAR
        )

        total_return_pct = None
        absolute_pnl = pnls["combined"]
        if not equity_curve.empty and float(equity_curve.iloc[0]) > 0:
            total_return_pct = float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0)
            absolute_pnl_mtm = float(equity_curve.iloc[-1] - equity_curve.iloc[0])
        else:
            absolute_pnl_mtm = None
            total_return_pct = (absolute_pnl / capital) if capital else None

        # Alpaca reconciliation
        portfolio_value = float(alpaca_account.get("portfolio_value") or 0) or None
        last_equity = float(equity_curve.iloc[-1]) if not equity_curve.empty else None
        open_unrealized = sum(
            float(p.get("unrealized_pl") or 0) for p in alpaca_positions
        )
        ledger_open = self.memory.get_open_trades()
        reconcile = {
            "alpaca_portfolio_value": portfolio_value,
            "alpaca_equity_curve_last": last_equity,
            "alpaca_open_unrealized_pl": open_unrealized,
            "local_closed_pnl": pnls["combined"],
            "local_open_positions": len(ledger_open),
            "alpaca_open_positions": len(
                [p for p in alpaca_positions if float(p.get("qty") or 0) != 0]
            ),
            "notes": [],
        }
        if portfolio_value is not None and last_equity is not None:
            gap = portfolio_value - last_equity
            if abs(gap) > max(1.0, 0.005 * portfolio_value):
                reconcile["notes"].append(
                    f"Alpaca portfolio_value (${portfolio_value:,.2f}) differs from "
                    f"history last equity (${last_equity:,.2f}) by ${gap:,.2f}."
                )

        rolling = rolling_performance(blended_returns, combined_pnl)

        report = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "conventions": {
                "risk_free_rate": RISK_FREE_RATE,
                "equity_periods_per_year": EQUITY_PERIODS_PER_YEAR,
                "crypto_periods_per_year": CRYPTO_PERIODS_PER_YEAR,
                "blended_periods_per_year": BLENDED_PERIODS_PER_YEAR,
                "blended_annualization": (
                    "Combined daily portfolio returns annualized with sqrt(365); "
                    "equity sleeve sqrt(252) on weekdays; crypto sleeve sqrt(365)."
                ),
                "blended_return_source": blended_source,
            },
            "evaluation": coverage,
            "returns": {
                "absolute_pnl_closed_trades": pnls["combined"],
                "absolute_pnl_equity": pnls["equity"],
                "absolute_pnl_crypto": pnls["crypto"],
                "absolute_pnl_mtm": absolute_pnl_mtm,
                "total_return_pct": total_return_pct,
                "total_return_pct_equity": (
                    (pnls["equity"] / capital) if capital else None
                ),
                "total_return_pct_crypto": (
                    (pnls["crypto"] / capital) if capital else None
                ),
                "capital_base": capital,
                "daily_returns_count": {
                    "blended": len(blended_returns),
                    "equity": len(equity_rets),
                    "crypto": len(crypto_rets),
                },
            },
            "risk_adjusted": {
                "sharpe": {
                    "blended": _flagged_ratio(
                        blended_sharpe, sufficient, len(blended_returns)
                    ),
                    "equity": _flagged_ratio(
                        equity_sharpe, sufficient, len(equity_rets)
                    ),
                    "crypto": _flagged_ratio(
                        crypto_sharpe, sufficient, len(crypto_rets)
                    ),
                },
                "sortino": {
                    "blended": _flagged_ratio(
                        blended_sortino, sufficient, len(blended_returns)
                    ),
                    "equity": _flagged_ratio(
                        equity_sortino, sufficient, len(equity_rets)
                    ),
                    "crypto": _flagged_ratio(
                        crypto_sortino, sufficient, len(crypto_rets)
                    ),
                },
                "max_drawdown_pct": _flagged_ratio(
                    max_dd, sufficient, len(equity_curve)
                ),
                "max_drawdown_duration_bars": {
                    "value": max_dd_bars,
                    "reliable": bool(sufficient and max_dd_bars is not None),
                    "reason": (
                        None
                        if sufficient and max_dd_bars is not None
                        else "Evaluation window too short or no equity curve."
                    ),
                },
            },
            "trade_stats": stats,
            "consistency": rolling,
            "alpaca_reconciliation": reconcile,
            "warnings": [coverage["note"]] if coverage.get("note") else [],
        }
        return report


def format_summary(report: dict[str, Any]) -> str:
    """Short performance block for a live demo. Detail lives in format_report."""
    ret = report.get("returns", {})
    ts = report.get("trade_stats", {})
    ev = report.get("evaluation", {})
    rec = report.get("alpaca_reconciliation", {})
    by_class = ts.get("by_asset_class") or {}

    def money_or_na(value) -> str:
        if not isinstance(value, (int, float)):
            return "n/a"
        return f"${value:,.2f}"

    avg_loss = ts.get("average_loss")
    if isinstance(avg_loss, (int, float)):
        loss_txt = f"-${abs(avg_loss):,.2f}"
    else:
        loss_txt = "n/a"
    win_rate = ts.get("win_rate")
    win_txt = f"{win_rate:.1%}" if isinstance(win_rate, (int, float)) else "n/a"
    pf = ts.get("profit_factor")
    if pf == float("inf"):
        pf_txt = "inf"
    elif isinstance(pf, (int, float)):
        pf_txt = f"{pf:.2f}"
    else:
        pf_txt = "n/a"

    lines = [
        "Performance",
        f"  Window          {ev.get('start') or '—'} to {ev.get('end') or '—'}",
        f"  Closed P&L      {money_or_na(ret.get('absolute_pnl_closed_trades'))}",
        f"  Stocks          {money_or_na(ret.get('absolute_pnl_equity'))}  "
        f"({by_class.get('equity', 0)} trades)",
        f"  Crypto          {money_or_na(ret.get('absolute_pnl_crypto'))}  "
        f"({by_class.get('crypto', 0)} trades)",
        f"  Trades          {ts.get('total_trades', 0)}   win rate {win_txt}",
        f"  Avg win / loss  {money_or_na(ts.get('average_win'))} / {loss_txt}",
        f"  Profit factor   {pf_txt}",
    ]
    portfolio = rec.get("alpaca_portfolio_value")
    if isinstance(portfolio, (int, float)):
        unreal = rec.get("alpaca_open_unrealized_pl")
        if isinstance(unreal, (int, float)) and unreal < 0:
            unreal_txt = f"-${abs(unreal):,.2f}"
        else:
            unreal_txt = money_or_na(unreal)
        lines.append(f"  Account value   {money_or_na(portfolio)}")
        lines.append(f"  Open P&L        {unreal_txt}")
    return "\n".join(lines)


def format_report(report: dict[str, Any]) -> str:
    """Full metrics dump, including ratio reliability notes."""
    lines = []
    lines.append("=" * 64)
    lines.append("PERFORMANCE REPORT")
    lines.append("=" * 64)

    ev = report.get("evaluation", {})
    lines.append(
        f"Window: {ev.get('start')} -> {ev.get('end')} "
        f"({ev.get('calendar_days')} days, {ev.get('weeks_with_data')} weeks)"
    )
    if ev.get("note"):
        lines.append(f"WARNING: {ev['note']}")

    conv = report.get("conventions", {})
    lines.append(
        f"Conventions: rf={conv.get('risk_free_rate')}, "
        f"equity√{conv.get('equity_periods_per_year')}, "
        f"crypto√{conv.get('crypto_periods_per_year')}, "
        f"blended√{conv.get('blended_periods_per_year')} "
        f"[{conv.get('blended_return_source')}]"
    )
    lines.append("-" * 64)

    ret = report.get("returns", {})

    def pct(x):
        return f"{x * 100:.2f}%" if isinstance(x, (int, float)) else "n/a"

    lines.append("RETURNS")
    lines.append(
        f"  Closed-trade P&L: ${ret.get('absolute_pnl_closed_trades', 0):,.2f} "
        f"(equity ${ret.get('absolute_pnl_equity', 0):,.2f} / "
        f"crypto ${ret.get('absolute_pnl_crypto', 0):,.2f})"
    )
    if ret.get("absolute_pnl_mtm") is not None:
        lines.append(f"  MTM P&L (Alpaca curve): ${ret['absolute_pnl_mtm']:,.2f}")
    lines.append(f"  Total return: {pct(ret.get('total_return_pct'))}")
    lines.append(
        f"  Sleeve return vs capital: equity {pct(ret.get('total_return_pct_equity'))} | "
        f"crypto {pct(ret.get('total_return_pct_crypto'))}"
    )
    lines.append("-" * 64)

    risk = report.get("risk_adjusted", {})

    def ratio_line(label, node):
        if not node:
            return f"  {label}: n/a"
        val = node.get("value")
        if val is None:
            return f"  {label}: n/a ({node.get('reason')})"
        flag = "" if node.get("reliable") else "  [UNRELIABLE]"
        return f"  {label}: {val:.3f}{flag}"

    lines.append("RISK-ADJUSTED")
    lines.append(ratio_line("Sharpe blended", risk.get("sharpe", {}).get("blended")))
    lines.append(ratio_line("Sharpe equity", risk.get("sharpe", {}).get("equity")))
    lines.append(ratio_line("Sharpe crypto", risk.get("sharpe", {}).get("crypto")))
    lines.append(ratio_line("Sortino blended", risk.get("sortino", {}).get("blended")))
    lines.append(ratio_line("Sortino equity", risk.get("sortino", {}).get("equity")))
    lines.append(ratio_line("Sortino crypto", risk.get("sortino", {}).get("crypto")))
    mdd = risk.get("max_drawdown_pct", {})
    if mdd.get("value") is not None:
        flag = "" if mdd.get("reliable") else "  [UNRELIABLE]"
        lines.append(f"  Max drawdown: {mdd['value'] * 100:.2f}%{flag}")
    else:
        lines.append("  Max drawdown: n/a")
    dur = risk.get("max_drawdown_duration_bars", {})
    lines.append(
        f"  Max DD duration (bars): {dur.get('value')} "
        f"{'' if dur.get('reliable') else '[UNRELIABLE]'}"
    )
    lines.append("-" * 64)

    ts = report.get("trade_stats", {})
    lines.append("TRADE STATS")
    wr = ts.get("win_rate")
    lines.append(
        f"  Trades: {ts.get('total_trades')} "
        f"(equity {ts.get('by_asset_class', {}).get('equity', 0)} / "
        f"crypto {ts.get('by_asset_class', {}).get('crypto', 0)})"
    )
    lines.append(f"  Win rate: {pct(wr)}")
    avg_w, avg_l = ts.get("average_win"), ts.get("average_loss")
    lines.append(
        f"  Avg win / avg loss: "
        f"{'$'+format(avg_w, ',.2f') if avg_w is not None else 'n/a'} / "
        f"{'$'+format(avg_l, ',.2f') if avg_l is not None else 'n/a'} "
        f"(ratio {ts.get('win_loss_ratio') if ts.get('win_loss_ratio') is not None else 'n/a'})"
    )
    pf = ts.get("profit_factor")
    pf_s = (
        "inf"
        if pf == float("inf")
        else (f"{pf:.3f}" if isinstance(pf, (int, float)) else "n/a")
    )
    lines.append(f"  Profit factor: {pf_s}")
    lines.append("-" * 64)

    cons = report.get("consistency", {})
    lines.append("CONSISTENCY (realized P&L by period)")
    weekly = cons.get("weekly") or []
    monthly = cons.get("monthly") or []
    if not weekly and not monthly:
        lines.append("  No period data yet.")
    else:
        lines.append(
            f"  Weeks with P&L: {len(weekly)} | Months with P&L: {len(monthly)}"
        )
        for row in weekly[-8:]:
            lines.append(f"    W {row['period']}: {row['pnl_or_return']:,.4f}")
        for row in monthly[-6:]:
            lines.append(f"    M {row['period']}: {row['pnl_or_return']:,.4f}")
    lines.append("-" * 64)

    rec = report.get("alpaca_reconciliation", {})
    lines.append("ALPACA RECONCILIATION")
    lines.append(
        f"  Portfolio value: {rec.get('alpaca_portfolio_value')} | "
        f"history last equity: {rec.get('alpaca_equity_curve_last')}"
    )
    lines.append(
        f"  Open positions: local {rec.get('local_open_positions')} / "
        f"alpaca {rec.get('alpaca_open_positions')} | "
        f"unrealized ${rec.get('alpaca_open_unrealized_pl')}"
    )
    for note in rec.get("notes") or []:
        lines.append(f"  NOTE: {note}")
    for warning in report.get("warnings") or []:
        lines.append(f"WARNING: {warning}")
    lines.append("=" * 64)
    return "\n".join(lines)


def report_to_json(report: dict[str, Any], indent: int = 2) -> str:
    def _default(obj):
        if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
            return None
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()
        raise TypeError(type(obj))

    return json.dumps(report, indent=indent, default=_default)
