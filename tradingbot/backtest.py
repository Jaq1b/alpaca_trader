"""Bar-replay backtester for strategy validation on historical Alpaca bars."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from tradingbot.sizing import (
    favorable_r,
    quantity_for_risk,
    tighten_stop,
    unrealized_pnl,
)
from tradingbot.snapshots import BarTable
from tradingbot.strategy import SignalStrategy
from tradingbot.universe import PRIORITY_SYMBOLS

logger = logging.getLogger(__name__)

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}$")


def warmup_calendar_days(timeframe: str, min_bars: int) -> int:
    """Calendar days of bars to load before the first trade day."""
    tf = (timeframe or "5Min").strip().lower()
    if tf in {"1day", "1d"}:
        return int(min_bars) + 10
    if tf in {"1hour", "4hour"}:
        return max(14, (int(min_bars) // 6) + 7)
    return 7


def parse_bound(value: Any, *, is_end: bool) -> datetime | None:
    """Parse a backtest bound. A date-only end includes that whole UTC day."""
    if value is None:
        return None
    date_only = False
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        date_only = True
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "~"}:
            return None
        if _DATE_ONLY.fullmatch(text):
            date_only = True
            dt = datetime.strptime(text, "%Y-%m-%d")
        else:
            raw = text[:-1] if text.endswith("Z") else text
            parsed = None
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                raise ValueError(f"Backtest date must be YYYY-MM-DD (got {value!r})")
            dt = parsed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if date_only and is_end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def resolve_window(
    *,
    lookback_days: int,
    start: Any = None,
    end: Any = None,
    now: datetime | None = None,
    warmup_days: int = 7,
) -> tuple[datetime, datetime, datetime, datetime]:
    """Return fetch_start, fetch_end, trade_start, trade_end in UTC.

    With no dates, the trade window is the last `lookback_days` ending now.
    Bars before trade_start are loaded so indicators are warm on day one.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    else:
        clock = clock.astimezone(timezone.utc)

    trade_start = parse_bound(start, is_end=False)
    trade_end = parse_bound(end, is_end=True)
    if trade_start is None and trade_end is None:
        trade_end = clock
        trade_start = clock - timedelta(days=max(int(lookback_days), 1))
    elif trade_start is None:
        trade_start = trade_end - timedelta(days=max(int(lookback_days), 1))
    elif trade_end is None:
        trade_end = clock
    if trade_start > trade_end:
        raise ValueError(
            "Backtest start "
            f"{trade_start.strftime('%Y-%m-%d')} is after end "
            f"{trade_end.strftime('%Y-%m-%d')}"
        )
    fetch_start = trade_start - timedelta(days=max(int(warmup_days), 0))
    return fetch_start, trade_end, trade_start, trade_end


def bar_epoch(ts) -> float:
    """UTC epoch seconds for a bar timestamp. Naive stamps are treated as UTC."""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return float(stamp.timestamp())


def in_window(ts, trade_start: datetime, trade_end: datetime) -> bool:
    stamp = pd.Timestamp(ts)
    start = pd.Timestamp(trade_start)
    end = pd.Timestamp(trade_end)
    if stamp.tzinfo is None:
        if start.tzinfo is not None:
            start = start.tz_convert("UTC").tz_localize(None)
        if end.tzinfo is not None:
            end = end.tz_convert("UTC").tz_localize(None)
    else:
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        start = start.tz_convert(stamp.tz)
        end = end.tz_convert(stamp.tz)
    return bool(start <= stamp <= end)


class InMemoryTradeStore:
    """Minimal memory stub so the strategy can check recent performance in backtests."""

    def __init__(self):
        self.trades: list[dict[str, Any]] = []

    def get_recent_performance(self, symbol: str, days: int = 7):
        # Replay uses last closed trades for the symbol (not wall-clock lookback).
        out = [
            _TradeView(row)
            for row in self.trades
            if row["symbol"] == symbol and row.get("status") != "open"
        ]
        return list(reversed(out[-10:]))

    def record_closed(self, trade: dict[str, Any]):
        self.trades.append(trade)


@dataclass
class _TradeView:
    _data: dict[str, Any]

    @property
    def pnl(self):
        return self._data.get("pnl", 0.0)

    @property
    def timestamp(self):
        return self._data.get("timestamp", "")


def format_elapsed(seconds: float) -> str:
    """Clock time for a run: 12.4s, 4m 14s, or 1h 2m 3s."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


@dataclass
class BacktestResult:
    initial_capital: float
    ending_capital: float
    total_pnl: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_pnl: float
    avg_r: float
    max_drawdown: float
    trades_per_day: float
    trades: list[dict[str, Any]] = field(default_factory=list)
    window_label: str = ""
    elapsed_seconds: float = 0.0
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["Backtest"]
        if self.window_label:
            lines.append(f"  window      {self.window_label}")
        lines.extend(
            [
                f"  capital     ${self.initial_capital:,.2f} → ${self.ending_capital:,.2f}",
                f"  P&L         ${self.total_pnl:,.2f}",
                f"  trades      {self.total_trades}  "
                f"wins {self.winning_trades}  losses {self.losing_trades}",
                f"  win rate    {self.win_rate:.1%}",
                f"  avg P&L     ${self.avg_pnl:,.2f}  avg R {self.avg_r:.2f}",
                f"  max DD      {self.max_drawdown:.1%}  trades/day {self.trades_per_day:.2f}",
            ]
        )
        if self.elapsed_seconds > 0:
            lines.append(f"  elapsed     {format_elapsed(self.elapsed_seconds)}")
        return "\n".join(lines)


class Backtester:
    def __init__(
        self,
        broker,
        config: dict[str, Any],
        initial_capital: float | None = None,
        start: Any = None,
        end: Any = None,
    ):
        self.broker = broker
        self.config = config
        bt = config.get("backtest", {})
        self.initial_capital = float(
            initial_capital
            if initial_capital is not None
            else bt.get("initial_capital", 10000.0)
        )
        self.lookback_days = int(bt.get("lookback_days", 30))
        self.timeframe = bt.get("timeframe", "5Min")
        min_bars = int(config.get("strategy", {}).get("min_bars", 40))
        chosen_start = start if start not in (None, "") else bt.get("start")
        chosen_end = end if end not in (None, "") else bt.get("end")
        (
            self.fetch_start,
            self.fetch_end,
            self.trade_start,
            self.trade_end,
        ) = resolve_window(
            lookback_days=self.lookback_days,
            start=chosen_start,
            end=chosen_end,
            warmup_days=warmup_calendar_days(self.timeframe, min_bars),
        )
        self.window_label = (
            f"{self.trade_start.strftime('%Y-%m-%d')} to "
            f"{self.trade_end.strftime('%Y-%m-%d')}"
        )
        self.risk_cfg = config.get("risk", {})
        self.strategy_cfg = config.get("strategy", {})
        stocks_cfg = config.get("symbols", {}).get("stocks", "watchlist")
        if isinstance(stocks_cfg, str) and stocks_cfg.strip().lower() == "watchlist":
            self.stock_symbols = list(PRIORITY_SYMBOLS)
        elif isinstance(stocks_cfg, list):
            self.stock_symbols = [str(s).upper() for s in stocks_cfg]
        else:
            self.stock_symbols = list(PRIORITY_SYMBOLS)
        self.crypto_symbols = list(config.get("symbols", {}).get("crypto", []))
        self.symbols = self.stock_symbols + self.crypto_symbols
        self.stock_risk = float(self.risk_cfg.get("stock_per_trade", 0.008))
        self.crypto_risk = float(self.risk_cfg.get("crypto_per_trade", 0.006))
        self.max_positions = int(self.risk_cfg.get("max_positions", 0))
        self.min_hold_bars = max(
            1, int(self.risk_cfg.get("min_hold_seconds", 300) / 300)
        )
        self.min_position_value = float(self.risk_cfg.get("min_position_value", 250.0))
        self.breakeven_r = float(self.strategy_cfg.get("breakeven_r", 1.0))
        self.trail_arm_r = float(self.strategy_cfg.get("trail_arm_r", 1.5))
        self.trail_distance_r = float(self.strategy_cfg.get("trail_distance_r", 1.0))
        self.allow_shorting = bool(self.risk_cfg.get("allow_shorting", True))

    def _asset_class(self, symbol: str) -> str:
        return "crypto" if symbol in self.crypto_symbols else "stock"

    def _load_history(self) -> dict[str, pd.DataFrame]:
        logger.info(
            f"Backtest window  {self.window_label}  "
            f"bars from {self.fetch_start.strftime('%Y-%m-%d')}"
        )
        history = {}
        for symbol in self.symbols:
            df = None
            for attempt in (1, 2):
                try:
                    df = self.broker.get_bars_range(
                        symbol,
                        timeframe=self.timeframe,
                        start=self.fetch_start,
                        end=self.fetch_end,
                    )
                    break
                except Exception as exc:
                    if attempt == 1:
                        logger.warning(f"{symbol} download failed, retrying: {exc}")
                        continue
                    logger.warning(f"No history for {symbol}: {exc}")
            if df is None:
                continue
            if df is not None and not df.empty:
                history[symbol] = df.sort_index()
            else:
                logger.warning(f"No history for {symbol}")
        if history:
            logger.info(
                f"Backtest data  {len(history)} symbols  "
                f"{sum(len(df) for df in history.values())} bars"
            )
        return history

    def run(self) -> BacktestResult:
        history = self._load_history()
        if not history:
            logger.error("No historical data available for backtest")
            return BacktestResult(
                self.initial_capital,
                self.initial_capital,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                window_label=self.window_label,
            )

        memory = InMemoryTradeStore()
        strategy = SignalStrategy(memory, self.strategy_cfg)
        books = {
            symbol: BarTable(frame, self.strategy_cfg, self._asset_class(symbol))
            for symbol, frame in history.items()
        }

        cash = self.initial_capital
        equity_peak = self.initial_capital
        max_drawdown = 0.0
        open_positions: dict[str, dict[str, Any]] = {}
        closed_trades: list[dict[str, Any]] = []
        daily_equity: dict[str, float] = {}

        all_ts = sorted({ts for df in history.values() for ts in df.index})
        window = max(self.strategy_cfg.get("min_bars", 40), 40)

        for ts in all_ts:
            if not in_window(ts, self.trade_start, self.trade_end):
                continue
            epoch = bar_epoch(ts)
            strategy.now = lambda epoch=epoch: epoch
            equity = cash
            for pos in open_positions.values():
                book = books[pos["symbol"]]
                loc = book.loc_of.get(ts)
                mark = float(book.close[loc]) if loc is not None else pos["entry_price"]
                qty = pos["quantity"]
                if pos.get("side") == "short":
                    # Cash already holds the reserved entry notional.
                    equity += (2 * pos["entry_price"] - mark) * qty
                else:
                    equity += mark * qty
            equity_peak = max(equity_peak, equity)
            if equity_peak > 0:
                max_drawdown = max(max_drawdown, (equity_peak - equity) / equity_peak)
            stamp = pd.Timestamp(ts)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert("UTC")
            daily_equity[stamp.date().isoformat()] = float(equity)

            for symbol, book in books.items():
                loc = book.loc_of.get(ts)
                if loc is None or loc < window:
                    continue

                price = float(book.close[loc])
                low = float(book.low[loc])
                high = float(book.high[loc])
                asset_class = book.asset_class
                snap = book.snapshot(loc, symbol)

                # Manage open position
                if symbol in open_positions:
                    pos = open_positions[symbol]
                    pos["bars_held"] += 1
                    side = pos.get("side", "long")

                    # Stop hit intrabar
                    stopped = (
                        low <= pos["stop_loss"]
                        if side == "long"
                        else high >= pos["stop_loss"]
                    )
                    if stopped:
                        exit_price = pos["stop_loss"]
                        self._close(
                            pos,
                            exit_price,
                            ts,
                            "Stop loss",
                            open_positions,
                            closed_trades,
                            memory,
                        )
                        if side == "long":
                            cash += exit_price * pos["quantity"]
                        else:
                            # Cover short: release margin + P&L vs entry
                            cash += (pos["entry_price"] - exit_price) * pos["quantity"]
                            cash += pos["entry_price"] * pos["quantity"]
                        continue

                    risk_unit = pos.get("risk_per_unit") or 0.0
                    pos["stop_loss"] = tighten_stop(
                        side=side,
                        entry_price=pos["entry_price"],
                        stop_loss=pos["stop_loss"],
                        price=price,
                        risk_per_unit=risk_unit,
                        breakeven_r=self.breakeven_r,
                        trail_arm_r=self.trail_arm_r,
                        trail_distance_r=self.trail_distance_r,
                    )

                    if pos["bars_held"] >= self.min_hold_bars:
                        r_now = favorable_r(pos["entry_price"], price, risk_unit, side)
                        abandon, abandon_reason = strategy.should_abandon(
                            None, symbol, asset_class, side, r_now, snapshot=snap
                        )
                        if abandon:
                            self._close(
                                pos,
                                price,
                                ts,
                                abandon_reason,
                                open_positions,
                                closed_trades,
                                memory,
                            )
                            if side == "long":
                                cash += price * pos["quantity"]
                            else:
                                cash += (pos["entry_price"] - price) * pos["quantity"]
                                cash += pos["entry_price"] * pos["quantity"]
                            continue
                        if side == "short":
                            should_exit, reason = strategy.should_cover(
                                None,
                                symbol,
                                pos["entry_price"],
                                asset_class,
                                snapshot=snap,
                            )
                        else:
                            should_exit, reason = strategy.should_sell(
                                None,
                                symbol,
                                pos["entry_price"],
                                asset_class,
                                snapshot=snap,
                            )
                        if should_exit:
                            self._close(
                                pos,
                                price,
                                ts,
                                reason,
                                open_positions,
                                closed_trades,
                                memory,
                            )
                            if side == "long":
                                cash += price * pos["quantity"]
                            else:
                                cash += (pos["entry_price"] - price) * pos["quantity"]
                                cash += pos["entry_price"] * pos["quantity"]
                    continue

                # Entries (max_positions <= 0 means no slot cap)
                if self.max_positions > 0 and len(open_positions) >= self.max_positions:
                    continue

                should_buy, reason, stop_loss, atr, score = strategy.should_buy(
                    None, symbol, asset_class, snapshot=snap
                )
                side = "long"
                if not should_buy or stop_loss <= 0:
                    if not (self.allow_shorting and asset_class == "stock"):
                        continue
                    should_short, reason, stop_loss, atr, score = strategy.should_short(
                        None, symbol, asset_class, snapshot=snap
                    )
                    if not should_short or stop_loss <= 0:
                        continue
                    side = "short"

                stop_distance = abs(price - stop_loss)
                if stop_distance <= 0:
                    continue

                max_val = (
                    self.risk_cfg.get("max_position_value_crypto", 5000)
                    if asset_class == "crypto"
                    else self.risk_cfg.get("max_position_value_stock", 10000)
                )
                risk_frac = (
                    self.crypto_risk if asset_class == "crypto" else self.stock_risk
                )
                qty = quantity_for_risk(
                    equity=cash,
                    risk_per_trade=risk_frac,
                    entry_price=price,
                    stop_loss=stop_loss,
                    asset_class=asset_class,
                    score=score,
                    score_min=strategy.entry_score_min(asset_class),
                    max_position_value=float(max_val),
                    min_position_value=self.min_position_value,
                )
                cost = qty * price
                if qty <= 0 or cost > cash:
                    continue

                # Long spends cash; short reserves notional as margin proxy
                cash -= cost
                open_positions[symbol] = {
                    "symbol": symbol,
                    "entry_price": price,
                    "quantity": qty,
                    "stop_loss": stop_loss,
                    "atr": atr,
                    "initial_risk": stop_distance * qty,
                    "risk_per_unit": stop_distance,
                    "timestamp": ts.isoformat(),
                    "asset_class": asset_class,
                    "reason": reason,
                    "bars_held": 0,
                    "side": side,
                }

        # Flatten leftovers at last price
        for symbol, pos in list(open_positions.items()):
            df = history[symbol]
            last_price = float(df.iloc[-1]["Close"])
            side = pos.get("side", "long")
            self._close(
                pos,
                last_price,
                df.index[-1],
                "End of backtest",
                open_positions,
                closed_trades,
                memory,
            )
            if side == "long":
                cash += last_price * pos["quantity"]
            else:
                cash += (pos["entry_price"] - last_price) * pos["quantity"]
                cash += pos["entry_price"] * pos["quantity"]

        wins = [t for t in closed_trades if t["pnl"] > 0]
        losses = [t for t in closed_trades if t["pnl"] <= 0]
        r_vals = [
            t["r_multiple"] for t in closed_trades if t.get("r_multiple") is not None
        ]
        days = max((self.trade_end - self.trade_start).total_seconds() / 86400, 1)
        total_pnl = sum(t["pnl"] for t in closed_trades)
        daily_equity[self.trade_end.date().isoformat()] = float(cash)
        equity_curve = [(day, daily_equity[day]) for day in sorted(daily_equity)]

        return BacktestResult(
            initial_capital=self.initial_capital,
            ending_capital=cash,
            total_pnl=total_pnl,
            total_trades=len(closed_trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=(len(wins) / len(closed_trades)) if closed_trades else 0.0,
            avg_pnl=(total_pnl / len(closed_trades)) if closed_trades else 0.0,
            avg_r=(sum(r_vals) / len(r_vals)) if r_vals else 0.0,
            max_drawdown=max_drawdown,
            trades_per_day=len(closed_trades) / days,
            trades=closed_trades,
            window_label=self.window_label,
            equity_curve=equity_curve,
        )

    def _close(
        self,
        pos: dict[str, Any],
        exit_price: float,
        ts,
        reason: str,
        open_positions: dict,
        closed_trades: list,
        memory: InMemoryTradeStore,
    ):
        side = pos.get("side", "long")
        pnl, pnl_pct = unrealized_pnl(
            pos["entry_price"], exit_price, pos["quantity"], side
        )
        initial_risk = pos.get("initial_risk") or 0.0
        r_multiple = (pnl / initial_risk) if initial_risk > 0 else None
        trade = {
            "symbol": pos["symbol"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "quantity": pos["quantity"],
            "timestamp": pos["timestamp"],
            "exit_timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "status": "closed",
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "r_multiple": r_multiple,
            "exit_reason": reason,
            "entry_reason": pos.get("reason"),
            "asset_class": pos["asset_class"],
            "side": side,
        }
        closed_trades.append(trade)
        memory.record_closed(trade)
        open_positions.pop(pos["symbol"], None)
