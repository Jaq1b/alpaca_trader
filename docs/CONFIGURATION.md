# Configuration

Everything the loop reads at startup is in `config.yaml`, unless an environment variable overrides it. Restart `python main.py run` after an edit. A process that is already running keeps the old file.

## Environment variables

Secrets belong in `.env`, which is loaded automatically and is gitignored.

| Variable | Required | Purpose |
| --- | --- | --- |
| `ALPACA_API_KEY` | Yes | Alpaca key id |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret |
| `ALPACA_PAPER_TRADING` | No | `true` or `false`. Overrides `paper_trading`. |
| `TRADING_CONFIG` | No | Path to a different YAML file. Default `config.yaml`. |
| `TRADING_DATA_DIR` | No | Overrides `data_dir`. |

Live trading is `paper_trading: false`, or `ALPACA_PAPER_TRADING=false`, plus live keys. The process prints a warning that it is using real money.

## Which names

| Key | Default | Meaning |
| --- | --- | --- |
| `symbols.stocks` | `watchlist` | `watchlist` is the 81 names in `tradingbot/universe.py`. A YAML list such as `["AAPL", "NVDA"]` trades only those tickers. `top_50` takes that many names from Alpaca's equity list, with the watchlist ordered first. It is not a market-cap ranking. `all` loads the equity universe. |
| `symbols.scan_batch_size` | `100` | How many stocks are requested per pass. Keep this at or above the watchlist length so every name is seen every pass. Larger lists rotate. |
| `symbols.crypto` | five USD pairs | Pairs the loop is allowed to buy. Use the `BTC/USD` form. |

## How big each order is

Fractions are of account equity. Buying power on a margin paper account is often about four times equity, and the bot ignores that when it sizes.

| Key | Default | Meaning |
| --- | --- | --- |
| `risk.stock_per_trade` | `0.008` | About 0.8% of equity is lost if a stock stop is hit. |
| `risk.crypto_per_trade` | `0.006` | About 0.6% of equity for a crypto stop. |
| `risk.max_position_value_stock` | `4000` | Dollar ceiling on one stock position. |
| `risk.max_position_value_crypto` | `2000` | Dollar ceiling on one crypto position. |
| `risk.min_position_value` | `250` | Orders smaller than this are skipped. |
| `risk.max_positions` | `0` | `0` means no cap on how many names can be open. Any positive number stops new entries at that count. |
| `risk.allow_shorting` | `true` | Stock shorts. Crypto stays long either way. |
| `risk.min_hold_seconds` | `300` | Minimum hold before a signal exit. Stops ignore this. |
| `risk.check_interval_seconds` | `45` | Pause between scans. |

A higher score than the minimum increases size, up to 1.3 times, inside `tradingbot/sizing.py`.

## When it enters and exits

| Key | Default | Meaning |
| --- | --- | --- |
| `strategy.buy_score_min` | `5` | Minimum score to buy or short a stock. |
| `strategy.buy_score_min_crypto` | `5` | Minimum score to buy crypto. |
| `strategy.sell_score_min` | `4` | Minimum score to exit on a signal. |
| `strategy.signal_cooldown` | `300` | Seconds before the same symbol can signal again. |
| `strategy.max_recent_losses` | `2` | Losing closes on that symbol in the past day before new entries stop. |
| `strategy.rsi_period` | `14` | RSI lookback. |
| `strategy.rsi_oversold` / `rsi_overbought` | `30` / `70` | Levels that award the strong RSI points. |
| `strategy.atr_period` | `14` | ATR lookback. |
| `strategy.atr_stop_mult` | `2.5` | Stop distance in ATRs. |
| `strategy.min_stop_pct_stock` | `0.008` | Stop is at least 0.8% from the stock entry. |
| `strategy.min_stop_pct_crypto` | `0.015` | Stop is at least 1.5% from the crypto entry. |
| `strategy.breakeven_r` | `1.0` | Open profit, in R, that moves the stop to the entry price. |
| `strategy.trail_arm_r` | `1.5` | Open profit, in R, that starts the trail. |
| `strategy.trail_distance_r` | `1.0` | Trail distance behind price, in R, once the trail is on. |
| `strategy.volume_mult_stock` / `volume_mult_crypto` | `1.4` / `1.25` | Volume must exceed this multiple of the recent average to score. |
| `strategy.take_profit_pct_stock` / `take_profit_pct_crypto` | `4` / `6` | Unrealized gain, in percent, that adds exit points. |
| `strategy.solid_profit_pct_stock` / `solid_profit_pct_crypto` | `2.5` / `4` | Smaller gain that adds fewer exit points. |
| `strategy.min_bars` | `40` | Bars required before a new entry is considered. |

Signal points, before the minimum is applied:

| Event | Points | Counts as a strong signal |
| --- | --- | --- |
| RSI oversold (long) or overbought (short) | 3 | Yes |
| MACD cross in the trade direction | 3 | Yes |
| 10-period SMA crossing the 20-period SMA | 3 | Yes |
| MACD already pointing the right way, no fresh cross | 1 | No |
| Price already on the right side of both SMAs | 1 | No |
| Volume above its recent average | 2 | No |

## Files and backtest window

| Key | Default | Meaning |
| --- | --- | --- |
| `paper_trading` | `true` | `true` uses `paper-api.alpaca.markets`. `false` uses the live API and real money. |
| `data_dir` | `trading_data` | SQLite file `trading.db` and the metrics JSON. This directory is gitignored. |
| `initial_capital` | `100000` | Fallback starting equity for return math when Alpaca history has no base value. Live sizing uses the account equity from the API. |
| `backtest.lookback_days` | `30` | Trade window when `start` and `end` are omitted: the last N days ending today. |
| `backtest.start` | empty | First day to open trades, `YYYY-MM-DD`. `--start` overrides this. |
| `backtest.end` | empty | Last day to open trades, `YYYY-MM-DD`. `--end` overrides this. Empty means today. |
| `backtest.initial_capital` | `10000` | Cash the replay starts with. Separate from `initial_capital` above. |
| `backtest.timeframe` | `5Min` | Bar size. The live loop also uses 5-minute bars. |

`cache` holds how long quotes, bars, and the account snapshot are reused inside one process.

## Commands

`python main.py` with no command is the same as `python main.py run`.

| Command | Places orders? |
| --- | --- |
| `python main.py status` | No |
| `python main.py report` | No |
| `python main.py run` | Yes |
| `python main.py backtest` | No |
| `python main.py montecarlo` | No |
| `pytest` | No |

Every command goes through `python main.py`.

### report flags

| Flag | What it changes |
| --- | --- |
| `--verbose` | Adds Sharpe, Sortino, drawdown, and the reconciliation block. Those ratios are marked unreliable until the ledger has at least 30 calendar days and 21 daily returns. |
| `--no-alpaca` | Skips the API. You only get numbers from the local database. |
| `--period` | How far back Alpaca portfolio history is requested. Default `3M`. Examples: `1D`, `1W`, `1M`, `1A`. |
| `--json-out` | Where the JSON copy is written. Default `trading_data/metrics_snapshot.json`. |

### backtest flags

| Flag | What it changes |
| --- | --- |
| `--start` | First day to open trades, `YYYY-MM-DD`. Overrides `backtest.start`. |
| `--end` | Last day to open trades, `YYYY-MM-DD`. Overrides `backtest.end`. Default is today. |

Bars from the week before `--start` are downloaded so the indicators are warm. New trades open only on the days between `--start` and `--end`.

### montecarlo flags

`montecarlo` accepts `--start` and `--end` with the same meaning as `backtest`. It replays once, then resamples the closed trades.

| Flag | What it changes |
| --- | --- |
| `--paths` | How many equity curves to build. Default `2000`. |
| `--seed` | Random seed. Default `1`, so the same trades repeat the same paths. |

## If it will not start

| What you see | What to do |
| --- | --- |
| `Set ALPACA_API_KEY and ALPACA_SECRET_KEY` | Copy `.env.example` to `.env`, fill in the paper keys, run from the repo root |
| `Could not reach Alpaca` | The keys are wrong, or live keys are being used while `paper_trading` is still `true` |
| `Account status is ...` and it is not `ACTIVE` | In the Alpaca dashboard, reset or create a paper account, then put the new keys in `.env` |
| `ModuleNotFoundError` | `source .venv/bin/activate`, then `pip install -r requirements.txt` |
| A ticker in `config.yaml` turns into `true` | Quote it. YAML reads bare `ON` as a boolean, so write `"ON"` |
| Report ratios say they are unreliable | The sample is still short. Use closed P&L, win rate, and profit factor until the window is long enough |
| Backtest prints no trades | The score filter did not fire in that window, or bar history came back empty. Try a shorter symbol list and confirm `status` can reach Alpaca |
