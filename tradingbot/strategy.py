"""Signal scoring, entry/exit decisions, and ATR stop placement."""

import time
from typing import Any

import pandas as pd

from tradingbot.indicators import atr, macd, rsi, sma
from tradingbot.memory import TradeMemory


class SignalStrategy:
    def __init__(self, memory: TradeMemory, config: dict[str, Any] | None = None):
        self.memory = memory
        self.cfg = config or {}
        self.last_signal_time: dict[str, float] = {}
        self.signal_cooldown = self.cfg.get("signal_cooldown", 300)

    def analyze_market(
        self, bars: pd.DataFrame, symbol: str, asset_class: str = "stock"
    ) -> dict:
        min_bars = self.cfg.get("min_bars", 40)
        if len(bars) < min_bars:
            return {"insufficient_data": True}

        price = float(bars["Close"].iloc[-1])
        rsi_period = self.cfg.get("rsi_period", 14)
        oversold = self.cfg.get("rsi_oversold", 30)
        overbought = self.cfg.get("rsi_overbought", 70)

        rsi_series = rsi(bars["Close"], rsi_period)
        rsi_value = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0
        snapshot = {
            "symbol": symbol,
            "asset_class": asset_class,
            "price": price,
            "rsi": {
                "value": rsi_value,
                "oversold": (
                    bool(rsi_value < oversold) if not rsi_series.empty else False
                ),
                "overbought": (
                    bool(rsi_value > overbought) if not rsi_series.empty else False
                ),
            },
        }

        macd_line, signal_line, histogram = macd(bars["Close"], 12, 26, 9)
        snapshot["macd"] = {
            "line": float(macd_line.iloc[-1]) if not macd_line.empty else 0.0,
            "signal": float(signal_line.iloc[-1]) if not signal_line.empty else 0.0,
            "histogram": float(histogram.iloc[-1]) if not histogram.empty else 0.0,
            "bullish_cross": False,
            "bearish_cross": False,
        }
        if len(macd_line) >= 2 and len(signal_line) >= 2:
            prev_macd, curr_macd = macd_line.iloc[-2], macd_line.iloc[-1]
            prev_signal, curr_signal = signal_line.iloc[-2], signal_line.iloc[-1]
            snapshot["macd"]["bullish_cross"] = bool(
                prev_macd <= prev_signal and curr_macd > curr_signal
            )
            snapshot["macd"]["bearish_cross"] = bool(
                prev_macd >= prev_signal and curr_macd < curr_signal
            )

        sma_10 = sma(bars["Close"], 10)
        sma_20 = sma(bars["Close"], 20)
        snapshot["sma"] = {
            "sma_10": float(sma_10.iloc[-1]) if not sma_10.empty else price,
            "sma_20": float(sma_20.iloc[-1]) if not sma_20.empty else price,
            "above_sma_10": (
                bool(price > sma_10.iloc[-1]) if not sma_10.empty else False
            ),
            "above_sma_20": (
                bool(price > sma_20.iloc[-1]) if not sma_20.empty else False
            ),
            "sma_cross_up": False,
            "sma_cross_down": False,
        }
        if len(sma_10) >= 2 and len(sma_20) >= 2:
            snapshot["sma"]["sma_cross_up"] = bool(
                sma_10.iloc[-2] <= sma_20.iloc[-2] and sma_10.iloc[-1] > sma_20.iloc[-1]
            )
            snapshot["sma"]["sma_cross_down"] = bool(
                sma_10.iloc[-2] >= sma_20.iloc[-2] and sma_10.iloc[-1] < sma_20.iloc[-1]
            )

        atr_series = atr(bars, self.cfg.get("atr_period", 14))
        atr_value = (
            float(atr_series.iloc[-1])
            if not atr_series.empty and pd.notna(atr_series.iloc[-1])
            else 0.0
        )
        snapshot["atr"] = atr_value
        snapshot["volume"] = self._volume_snapshot(bars, asset_class)
        return snapshot

    def _volume_snapshot(self, bars: pd.DataFrame, asset_class: str) -> dict:
        if len(bars) < 10:
            return {"high_volume": False, "current": 0, "average": 0}

        avg_volume = bars["Volume"].rolling(10).mean().iloc[-1]
        current_volume = bars["Volume"].iloc[-1]
        multiplier = (
            self.cfg.get("volume_mult_crypto", 1.5)
            if asset_class == "crypto"
            else self.cfg.get("volume_mult_stock", 1.4)
        )
        return {
            "current": float(current_volume),
            "average": float(avg_volume),
            "high_volume": bool(current_volume > avg_volume * multiplier),
        }

    def _atr_stop(
        self, price: float, atr: float, asset_class: str, side: str = "long"
    ) -> float:
        mult = self.cfg.get("atr_stop_mult", 1.8)
        if atr <= 0:
            pad = 0.035 if asset_class == "crypto" else 0.022
            return price * (1 - pad) if side == "long" else price * (1 + pad)

        if side == "long":
            stop = price - (mult * atr)
            floor = price * (0.90 if asset_class == "crypto" else 0.95)
            return max(stop, floor)

        stop = price + (mult * atr)
        ceiling = price * (1.10 if asset_class == "crypto" else 1.05)
        return min(stop, ceiling)

    def _entry_blocked(self, symbol: str) -> str | None:
        recent = self.memory.get_recent_performance(symbol, days=1)
        losses = sum(1 for trade in recent if trade.pnl < 0)
        max_losses = self.cfg.get("max_recent_losses", 3)
        if losses >= max_losses:
            return f"Too many recent losses ({losses})"

        last = self.last_signal_time.get(symbol)
        if last is not None and time.time() - last < self.signal_cooldown:
            return "Signal cooldown active"
        return None

    def should_buy(
        self, bars: pd.DataFrame, symbol: str, asset_class: str = "stock"
    ) -> tuple[bool, str, float, float]:
        snapshot = self.analyze_market(bars, symbol, asset_class)
        if snapshot.get("insufficient_data"):
            return False, "Insufficient data", 0.0, 0.0

        blocked = self._entry_blocked(symbol)
        if blocked:
            return False, blocked, 0.0, 0.0

        if not snapshot["sma"]["above_sma_20"] and not snapshot["rsi"]["oversold"]:
            return False, "Below SMA 20 without oversold bounce", 0.0, 0.0

        signals: list[str] = []
        score = 0
        strong = False

        if snapshot["rsi"]["oversold"]:
            signals.append("RSI oversold")
            score += 3
            strong = True

        if snapshot["macd"]["bullish_cross"]:
            signals.append("MACD bullish cross")
            score += 3
            strong = True
        elif (
            snapshot["macd"]["histogram"] > 0
            and snapshot["macd"]["line"] > snapshot["macd"]["signal"]
        ):
            signals.append("MACD momentum up")
            score += 1

        if snapshot["sma"]["sma_cross_up"]:
            signals.append("SMA cross up")
            score += 3
            strong = True
        elif snapshot["sma"]["above_sma_10"] and snapshot["sma"]["above_sma_20"]:
            signals.append("Above SMA 10 & 20")
            score += 1

        if snapshot["volume"]["high_volume"]:
            signals.append("High volume")
            score += 2

        stop = self._atr_stop(snapshot["price"], snapshot["atr"], asset_class, "long")
        required = self.cfg.get("buy_score_min", 4)
        if score >= required and strong:
            self.last_signal_time[symbol] = time.time()
            return (
                True,
                f"BUY: {', '.join(signals)} (Score: {score})",
                stop,
                snapshot["atr"],
            )

        return False, f"Weak buy: {', '.join(signals)} (Score: {score})", 0.0, 0.0

    def should_short(
        self, bars: pd.DataFrame, symbol: str, asset_class: str = "stock"
    ) -> tuple[bool, str, float, float]:
        if asset_class == "crypto":
            return False, "Crypto shorts disabled", 0.0, 0.0

        snapshot = self.analyze_market(bars, symbol, asset_class)
        if snapshot.get("insufficient_data"):
            return False, "Insufficient data", 0.0, 0.0

        blocked = self._entry_blocked(symbol)
        if blocked:
            return False, blocked, 0.0, 0.0

        # Skip shorts in a clear uptrend unless RSI is already stretched.
        if snapshot["sma"]["above_sma_20"] and not snapshot["rsi"]["overbought"]:
            return False, "Above SMA 20 without overbought stretch", 0.0, 0.0

        signals: list[str] = []
        score = 0
        strong = False

        if snapshot["rsi"]["overbought"]:
            signals.append("RSI overbought")
            score += 3
            strong = True

        if snapshot["macd"]["bearish_cross"]:
            signals.append("MACD bearish cross")
            score += 3
            strong = True
        elif (
            snapshot["macd"]["histogram"] < 0
            and snapshot["macd"]["line"] < snapshot["macd"]["signal"]
        ):
            signals.append("MACD momentum down")
            score += 1

        if snapshot["sma"]["sma_cross_down"]:
            signals.append("SMA cross down")
            score += 3
            strong = True
        elif (
            not snapshot["sma"]["above_sma_10"] and not snapshot["sma"]["above_sma_20"]
        ):
            signals.append("Below SMA 10 & 20")
            score += 1

        if snapshot["volume"]["high_volume"]:
            signals.append("High volume")
            score += 2

        stop = self._atr_stop(snapshot["price"], snapshot["atr"], asset_class, "short")
        required = self.cfg.get("buy_score_min", 4)
        if score >= required and strong:
            self.last_signal_time[symbol] = time.time()
            return (
                True,
                f"SHORT: {', '.join(signals)} (Score: {score})",
                stop,
                snapshot["atr"],
            )

        return False, f"Weak short: {', '.join(signals)} (Score: {score})", 0.0, 0.0

    def should_sell(
        self,
        bars: pd.DataFrame,
        symbol: str,
        entry_price: float | None = None,
        asset_class: str = "stock",
    ) -> tuple[bool, str]:
        return self._should_exit(bars, symbol, entry_price, asset_class, side="long")

    def should_cover(
        self,
        bars: pd.DataFrame,
        symbol: str,
        entry_price: float | None = None,
        asset_class: str = "stock",
    ) -> tuple[bool, str]:
        return self._should_exit(bars, symbol, entry_price, asset_class, side="short")

    def _should_exit(
        self,
        bars: pd.DataFrame,
        symbol: str,
        entry_price: float,
        asset_class: str,
        side: str,
    ) -> tuple[bool, str]:
        snapshot = self.analyze_market(bars, symbol, asset_class)
        if snapshot.get("insufficient_data"):
            return False, "Insufficient data"

        last = self.last_signal_time.get(symbol)
        if last is not None and time.time() - last < self.signal_cooldown:
            return False, "Signal cooldown active"

        signals: list[str] = []
        score = 0
        label = "SELL" if side == "long" else "COVER"

        if side == "long":
            if snapshot["rsi"]["overbought"]:
                signals.append("RSI overbought")
                score += 3
            elif snapshot["rsi"]["value"] > 65:
                signals.append("RSI elevated")
                score += 1

            if snapshot["macd"]["bearish_cross"]:
                signals.append("MACD bearish cross")
                score += 3
            elif (
                snapshot["macd"]["histogram"] < 0
                and snapshot["macd"]["line"] < snapshot["macd"]["signal"]
            ):
                signals.append("MACD momentum down")
                score += 1

            if snapshot["sma"]["sma_cross_down"]:
                signals.append("SMA cross down")
                score += 3
            elif (
                not snapshot["sma"]["above_sma_10"]
                and not snapshot["sma"]["above_sma_20"]
            ):
                signals.append("Below both SMAs")
                score += 2
        else:
            if snapshot["rsi"]["oversold"]:
                signals.append("RSI oversold")
                score += 3
            elif snapshot["rsi"]["value"] < 35:
                signals.append("RSI washed out")
                score += 1

            if snapshot["macd"]["bullish_cross"]:
                signals.append("MACD bullish cross")
                score += 3
            elif (
                snapshot["macd"]["histogram"] > 0
                and snapshot["macd"]["line"] > snapshot["macd"]["signal"]
            ):
                signals.append("MACD momentum up")
                score += 1

            if snapshot["sma"]["sma_cross_up"]:
                signals.append("SMA cross up")
                score += 3
            elif snapshot["sma"]["above_sma_10"] and snapshot["sma"]["above_sma_20"]:
                signals.append("Above both SMAs")
                score += 2

        if entry_price:
            price = snapshot["price"]
            if side == "long":
                profit_pct = (price - entry_price) / entry_price * 100
            else:
                profit_pct = (entry_price - price) / entry_price * 100

            take_profit = (
                self.cfg.get("take_profit_pct_crypto", 6.0)
                if asset_class == "crypto"
                else self.cfg.get("take_profit_pct_stock", 4.0)
            )
            solid_profit = (
                self.cfg.get("solid_profit_pct_crypto", 4.0)
                if asset_class == "crypto"
                else self.cfg.get("solid_profit_pct_stock", 2.5)
            )
            if profit_pct >= take_profit:
                signals.append(f"Take profit ({profit_pct:.1f}%)")
                score += 3
            elif profit_pct >= solid_profit:
                signals.append(f"Solid profit ({profit_pct:.1f}%)")
                score += 1

        required = self.cfg.get("sell_score_min", 3)
        if score >= required:
            self.last_signal_time[symbol] = time.time()
            return True, f"{label}: {', '.join(signals)} (Score: {score})"

        return False, f"Weak {label.lower()}: {', '.join(signals)} (Score: {score})"
