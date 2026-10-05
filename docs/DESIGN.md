# Design

Each choice below is the one in the code today.

## Alpaca IEX bars

**Chose:** Live prices and bars come from Alpaca’s market-data REST endpoints, with `feed=iex` for equities (`tradingbot/broker.py`). Crypto uses Alpaca’s crypto bars.

**Why:** Signals and fills stay on the same broker. IEX is the equity feed that paper accounts can use.

**Trade-off:** IEX is not the full consolidated tape. Some prints differ from a paid SIP feed.

## Watchlist scan with a short cache

**Chose:** A small TTL cache on bars, prices, account, and positions. The default universe is the curated list in `tradingbot/universe.py` (`symbols.stocks: watchlist`). When that list fits in `scan_batch_size`, every name is requested every pass. Multi-symbol bar requests go out in chunks.

**Why:** A rotating slice of the whole market spent cycles on names that never scored. A liquid list that fits in one pass is what the loop actually watches.

**Trade-off:** A cached close can be as old as `bars_ttl_seconds`. Names outside the watchlist are ignored until you list them or, on the live loop, switch `symbols.stocks` to `top_N` or `all`. A replay uses the watchlist or an explicit ticker list.

## ATR stop, then size from that distance

**Chose:** The stop distance is `max(atr_stop_mult × ATR, min_stop_pct × price)` (`strategy._atr_stop`). A long stop is then kept inside 5% of price for stocks and 10% for crypto (shorts use the same band above price). An entry is skipped when 2.5× ATR is inside that minimum, because the stop would be a fixed percent in a quieter market. Quantity is `equity × risk_per_trade × conviction / |entry − stop|`, then capped by `max_position_value_*` (`sizing.quantity_for_risk`). Conviction rises with score above the entry minimum, up to 1.3×. After +1R the stop moves to entry. After +1.5R it trails 1R behind price (`sizing.tighten_stop`). A loser whose MACD or SMA cross flips closes before the stop.

**Why:** The dollar risk of a full stop stays near a fixed fraction of equity. The minimum width keeps a quiet 5-minute ATR from placing the stop a few tenths of a percent away. Skipping those quiet names, and managing the stop in R, is what keeps a 0.8% floor from turning every trade into a full -1R or a tenth-of-an-R scratch.

**Trade-off:** ATR widens in a fast market, so size shrinks as the range grows. There is no position-count cap (`max_positions: 0`). New entries stop when equity or the per-trade dollar cap says so. Sizing uses equity, not buying power.

## Score, plus one strong signal

**Chose:** RSI, MACD, SMA, and volume add points. An entry is sent only when the score reaches `buy_score_min` (or `buy_score_min_crypto`) and at least one strong event fired: an RSI extreme, a MACD cross, or an SMA cross. A long below the 20-period average is skipped unless RSI is oversold. A short above that average is skipped unless RSI is overbought. Crypto is long only.

**Why:** A single indicator opened too often. Requiring every indicator at once almost never fired. The score plus one strong event is the gate in `SignalStrategy`.

**Trade-off:** The weights are set by hand. `python main.py backtest` replays them. This repo does not fit the weights.

## Stocks on the session clock, crypto all day

**Chose:** Equities open and close only while Alpaca’s clock says the regular session is open, with day time-in-force and integer quantity. Crypto is treated as always open, uses GTC notional market buys, and rewrites the fill quantity from `filled_qty`. Crypto sells use that exact quantity.

**Why:** US equities and Alpaca spot crypto differ in hours, order type, and fractional size.

**Trade-off:** Stock orders are not polled for a fill the way crypto buys are. Crypto shorts are off. If the Alpaca calendar is unreachable, the session check falls back to a short local holiday list.

## SQLite ledger, Alpaca positions on restart

**Chose:** Trades and events live in `trading_data/trading.db` (`tradingbot/memory.py`). On startup, `_restore_state` keeps ledger rows that match a live Alpaca position and closes the rest as “Closed — not on Alpaca” with flat P&L.

**Why:** The report needs a local history. The broker still owns the book. A crash or a paper-account reset must not leave the ledger holding shares Alpaca does not.

**Trade-off:** One writer, one disk. A flat ghost close hides P&L if the position was flattened outside the bot. Sharpe and Sortino in `tradingbot/metrics.py` use a 0% risk-free rate (equity √252, crypto and blended √365) and stay marked unreliable until the sample has 30 calendar days and 21 daily returns.
