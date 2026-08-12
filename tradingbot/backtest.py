"""Bar-replay backtester for strategy validation on historical Alpaca bars."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from tradingbot.strategy import SignalStrategy

logger = logging.getLogger(__name__)


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

    def summary(self) -> str:
        return "\n".join(
            [
                "=" * 50,
                "BACKTEST RESULTS",
                "=" * 50,
                f"Capital: ${self.initial_capital:,.2f} -> ${self.ending_capital:,.2f}",
                f"Total P&L: ${self.total_pnl:,.2f}",
                f"Trades: {self.total_trades} | Wins: {self.winning_trades} | "
                f"Losses: {self.losing_trades}",
                f"Win rate: {self.win_rate:.1%}",
                f"Avg P&L / trade: ${self.avg_pnl:,.2f}",
                f"Avg R-multiple: {self.avg_r:.2f}",
                f"Max drawdown: {self.max_drawdown:.1%}",
                f"Trades / day: {self.trades_per_day:.2f}",
                "=" * 50,
            ]
        )


class Backtester:
    def __init__(
        self,
        broker,
        config: dict[str, Any],
        initial_capital: float | None = None,
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
        self.bar_limit = int(bt.get("bar_limit", 5000))
        self.risk_cfg = config.get("risk", {})
        self.strategy_cfg = config.get("strategy", {})
        self.stock_symbols = list(config.get("symbols", {}).get("stocks", []))
        self.crypto_symbols = list(config.get("symbols", {}).get("crypto", []))
        self.symbols = self.stock_symbols + self.crypto_symbols
        self.risk_per_trade = float(self.risk_cfg.get("stock_per_trade", 0.012))
        self.max_positions = int(self.risk_cfg.get("max_positions", 6))
        self.min_hold_bars = max(
            1, int(self.risk_cfg.get("min_hold_seconds", 180) / 300)
        )
        self.atr_trail_mult = float(self.strategy_cfg.get("atr_trail_mult", 2.0))
        self.trail_arm_atr_mult = float(
            self.strategy_cfg.get("trail_arm_atr_mult", 1.5)
        )
        self.allow_shorting = bool(self.risk_cfg.get("allow_shorting", True))

    def _asset_class(self, symbol: str) -> str:
        return "crypto" if symbol in self.crypto_symbols else "stock"

    def _load_history(self) -> dict[str, pd.DataFrame]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.lookback_days)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        history = {}
        for symbol in self.symbols:
            df = self.broker.get_bars(
                symbol,
                timeframe=self.timeframe,
                limit=self.bar_limit,
                start=start_str,
                end=end_str,
                force=True,
            )
            if df is not None and not df.empty:
                history[symbol] = df.sort_index()
                logger.info(f"Loaded {len(df)} bars for {symbol}")
            else:
                logger.warning(f"No history for {symbol}")
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
            )

        memory = InMemoryTradeStore()
        strategy = SignalStrategy(memory, self.strategy_cfg)

        cash = self.initial_capital
        equity_peak = self.initial_capital
        max_drawdown = 0.0
        open_positions: dict[str, dict[str, Any]] = {}
        closed_trades: list[dict[str, Any]] = []

        all_ts = sorted({ts for df in history.values() for ts in df.index})
        window = max(self.strategy_cfg.get("min_bars", 40), 40)

        for i, ts in enumerate(all_ts):
            equity = cash
            for pos in open_positions.values():
                df = history[pos["symbol"]]
                if ts in df.index:
                    equity += float(df.loc[ts]["Close"]) * pos["quantity"]
                else:
                    equity += pos["entry_price"] * pos["quantity"]
            equity_peak = max(equity_peak, equity)
            if equity_peak > 0:
                max_drawdown = max(max_drawdown, (equity_peak - equity) / equity_peak)

            for symbol, df in history.items():
                if ts not in df.index:
                    continue
                loc = df.index.get_loc(ts)
                if isinstance(loc, slice):
                    continue
                if loc < window:
                    continue

                window_df = df.iloc[: loc + 1].tail(120)
                bar = df.iloc[loc]
                price = float(bar["Close"])
                low = float(bar["Low"])
                asset_class = self._asset_class(symbol)

                # Manage open position
                if symbol in open_positions:
                    pos = open_positions[symbol]
                    pos["bars_held"] += 1
                    side = pos.get("side", "long")
                    high = float(bar["High"])

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

                    # ATR trail
                    atr = pos.get("atr") or 0.0
                    if side == "long":
                        profit = price - pos["entry_price"]
                        if atr > 0 and profit >= self.trail_arm_atr_mult * atr:
                            new_stop = price - self.atr_trail_mult * atr
                            pos["stop_loss"] = max(pos["stop_loss"], new_stop)
                    else:
                        profit = pos["entry_price"] - price
                        if atr > 0 and profit >= self.trail_arm_atr_mult * atr:
                            new_stop = price + self.atr_trail_mult * atr
                            pos["stop_loss"] = min(pos["stop_loss"], new_stop)

                    if pos["bars_held"] >= self.min_hold_bars:
                        if side == "short":
                            should_exit, reason = strategy.should_cover(
                                window_df, symbol, pos["entry_price"], asset_class
                            )
                        else:
                            should_exit, reason = strategy.should_sell(
                                window_df, symbol, pos["entry_price"], asset_class
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

                # Entries
                if len(open_positions) >= self.max_positions:
                    continue

                should_buy, reason, stop_loss, atr = strategy.should_buy(
                    window_df, symbol, asset_class
                )
                side = "long"
                if not should_buy or stop_loss <= 0:
                    if not (self.allow_shorting and asset_class == "stock"):
                        continue
                    should_short, reason, stop_loss, atr = strategy.should_short(
                        window_df, symbol, asset_class
                    )
                    if not should_short or stop_loss <= 0:
                        continue
                    side = "short"

                stop_distance = abs(price - stop_loss)
                if stop_distance <= 0:
                    continue

                risk_amount = cash * self.risk_per_trade
                qty = risk_amount / stop_distance
                max_val = (
                    self.risk_cfg.get("max_position_value_crypto", 200)
                    if asset_class == "crypto"
                    else self.risk_cfg.get("max_position_value_stock", 300)
                )
                qty = min(qty, max_val / price)
                if asset_class != "crypto":
                    qty = max(1, int(qty))
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
        days = max(self.lookback_days, 1)
        total_pnl = sum(t["pnl"] for t in closed_trades)

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
        if side == "short":
            pnl = (pos["entry_price"] - exit_price) * pos["quantity"]
            pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100
        else:
            pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
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
