"""One-pass indicator values for the replay.

Each value at bar N uses only bars through N, and only the same trailing
window the replay used to pass into ``analyze_market`` (the last ``TAIL_BARS``
rows). Rolling indicators match a full-series rolling window. MACD is an EMA,
so it is recomputed on that trailing window rather than on the whole series.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradingbot.indicators import atr, rsi, sma

# Must match Backtester's `df.iloc[: loc + 1].tail(120)`.
TAIL_BARS = 120


class BarTable:
    """Arrays aligned to a symbol's bar frame, oldest row at index 0."""

    def __init__(self, frame: pd.DataFrame, cfg: dict, asset_class: str):
        self.asset_class = asset_class
        self.min_bars = int(cfg.get("min_bars", 40))
        self.oversold = float(cfg.get("rsi_oversold", 30))
        self.overbought = float(cfg.get("rsi_overbought", 70))
        self.vol_mult = float(
            cfg.get("volume_mult_crypto", 1.25)
            if asset_class == "crypto"
            else cfg.get("volume_mult_stock", 1.4)
        )
        close = frame["Close"]
        self.close = close.to_numpy(dtype=np.float64, copy=False)
        self.high = frame["High"].to_numpy(dtype=np.float64, copy=False)
        self.low = frame["Low"].to_numpy(dtype=np.float64, copy=False)
        volume = frame["Volume"].to_numpy(dtype=np.float64, copy=False)
        self.volume = volume
        self.vol_avg = (
            frame["Volume"].rolling(10).mean().to_numpy(dtype=np.float64, copy=False)
        )
        rsi_period = int(cfg.get("rsi_period", 14))
        atr_period = int(cfg.get("atr_period", 14))
        self.rsi = rsi(close, rsi_period).to_numpy(dtype=np.float64, copy=False)
        self.atr = atr(frame, atr_period).to_numpy(dtype=np.float64, copy=False)
        self.sma10 = sma(close, 10).to_numpy(dtype=np.float64, copy=False)
        self.sma20 = sma(close, 20).to_numpy(dtype=np.float64, copy=False)
        (
            self.macd_line,
            self.macd_line_prev,
            self.macd_signal,
            self.macd_signal_prev,
            self.macd_hist,
        ) = _sliding_macd(self.close, TAIL_BARS)
        self.loc_of = {ts: i for i, ts in enumerate(frame.index)}

    def snapshot(self, loc: int, symbol: str) -> dict:
        """Same dict ``analyze_market`` builds for the trailing window at ``loc``."""
        if loc + 1 < self.min_bars:
            return {"insufficient_data": True}

        price = float(self.close[loc])
        rsi_value = float(self.rsi[loc])
        sma10 = float(self.sma10[loc])
        sma20 = float(self.sma20[loc])
        prev10 = float(self.sma10[loc - 1])
        prev20 = float(self.sma20[loc - 1])
        line = float(self.macd_line[loc])
        signal = float(self.macd_signal[loc])
        prev_line = float(self.macd_line_prev[loc])
        prev_signal = float(self.macd_signal_prev[loc])
        atr_raw = self.atr[loc]
        atr_value = float(atr_raw) if atr_raw == atr_raw else 0.0
        current_volume = float(self.volume[loc])
        average_volume = float(self.vol_avg[loc])

        return {
            "symbol": symbol,
            "asset_class": self.asset_class,
            "price": price,
            "rsi": {
                "value": rsi_value,
                "oversold": bool(rsi_value < self.oversold),
                "overbought": bool(rsi_value > self.overbought),
            },
            "macd": {
                "line": line,
                "signal": signal,
                "histogram": float(self.macd_hist[loc]),
                "bullish_cross": bool(prev_line <= prev_signal and line > signal),
                "bearish_cross": bool(prev_line >= prev_signal and line < signal),
            },
            "sma": {
                "sma_10": sma10,
                "sma_20": sma20,
                "above_sma_10": bool(price > sma10),
                "above_sma_20": bool(price > sma20),
                "sma_cross_up": bool(prev10 <= prev20 and sma10 > sma20),
                "sma_cross_down": bool(prev10 >= prev20 and sma10 < sma20),
            },
            "atr": atr_value,
            "volume": {
                "current": current_volume,
                "average": average_volume,
                "high_volume": bool(current_volume > average_volume * self.vol_mult),
            },
        }


def _sliding_macd(
    close: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """MACD endpoints on each bar's trailing window.

    ``line_prev`` / ``signal_prev`` are the prior point inside that same
    window, which is what a cross on ``tail(window)`` compares. They are not
    the previous bar's full-window MACD.
    """
    n = close.shape[0]
    line = np.empty(n, dtype=np.float64)
    line_prev = np.empty(n, dtype=np.float64)
    signal = np.empty(n, dtype=np.float64)
    signal_prev = np.empty(n, dtype=np.float64)
    hist = np.empty(n, dtype=np.float64)
    # pandas ewm(span) uses alpha = 2 / (span + 1), adjust=True.
    decay_fast = 1.0 - 2.0 / 13.0
    decay_slow = 1.0 - 2.0 / 27.0
    decay_signal = 1.0 - 2.0 / 10.0
    buf = np.empty(window, dtype=np.float64)

    for i in range(n):
        start = 0 if i < window - 1 else i - (window - 1)
        length = i - start + 1
        num_fast = num_slow = 0.0
        den_fast = den_slow = 0.0
        for t in range(length):
            value = close[start + t]
            if t == 0:
                num_fast = num_slow = value
                den_fast = den_slow = 1.0
            else:
                num_fast = value + decay_fast * num_fast
                den_fast = 1.0 + decay_fast * den_fast
                num_slow = value + decay_slow * num_slow
                den_slow = 1.0 + decay_slow * den_slow
            buf[t] = num_fast / den_fast - num_slow / den_slow

        num_sig = den_sig = 0.0
        prev_sig = curr_sig = 0.0
        for t in range(length):
            value = buf[t]
            if t == 0:
                num_sig = value
                den_sig = 1.0
            else:
                num_sig = value + decay_signal * num_sig
                den_sig = 1.0 + decay_signal * den_sig
            prev_sig = curr_sig
            curr_sig = num_sig / den_sig

        line[i] = buf[length - 1]
        signal[i] = curr_sig
        hist[i] = line[i] - signal[i]
        if length > 1:
            line_prev[i] = buf[length - 2]
            signal_prev[i] = prev_sig
        else:
            line_prev[i] = np.nan
            signal_prev[i] = np.nan

    return line, line_prev, signal, signal_prev, hist
