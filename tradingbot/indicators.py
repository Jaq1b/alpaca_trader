"""SMA, EMA, RSI, MACD, and ATR — building blocks for signal scoring."""


import pandas as pd


def sma(closes: pd.Series, window: int) -> pd.Series:
    return closes.rolling(window=window).mean()


def ema(closes: pd.Series, window: int) -> pd.Series:
    return closes.ewm(span=window).mean()


def rsi(closes: pd.Series, window: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(closes, fast) - ema(closes, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def atr(bars: pd.DataFrame, window: int = 14) -> pd.Series:
    prev_close = bars["Close"].shift(1)
    true_range = pd.concat(
        [
            (bars["High"] - bars["Low"]).abs(),
            (bars["High"] - prev_close).abs(),
            (bars["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=window).mean()
