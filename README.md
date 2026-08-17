# tradingbottest

Alpaca paper-trading bot for US equities and crypto. It scores RSI / MACD / SMA / volume setups, sizes from ATR stop distance, and keeps a SQLite ledger plus a performance report.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Paper trading](https://img.shields.io/badge/default-paper%20trading-brightgreen.svg)](https://app.alpaca.markets/paper/dashboard/overview)
[![Tests](https://img.shields.io/badge/tests-pytest-informational.svg)](tests/test_metrics.py)

> **Paper by default.** Live trading is opt-in and risks real money.

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Core concepts](#core-concepts)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

## Features

| Area | What it does |
| --- | --- |
| Markets | Curated liquid watchlist (full scan each loop) + BTC, ETH, SOL, XRP, DOGE |
| Signals | RSI, MACD, SMA, volume — long and short stock setups; crypto long-only |
| Risk | ATR stops with a minimum width, size from stop distance × conviction, per-trade max notional |
| Execution | Long BUY / short SELL; cover with BUY |
| Storage | SQLite trades + event log under `trading_data/` |
| Metrics | Sharpe, Sortino, drawdown, win rate (`scripts/report.py`) |
| Replay | Historical bar backtest (`python backtest.py`) |

## Quick start

**Requirements:** Python 3.10+, free [Alpaca](https://alpaca.markets/) paper account.

### 1. Clone and install

```bash
git clone https://github.com/<your-user>/tradingbottest.git
cd tradingbottest

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Add paper API keys

Create keys in the [Paper Trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) (keys usually start with `PK`).

```bash
cp .env.example .env
```

Edit `.env` (use double quotes):

```env
ALPACA_API_KEY="your_paper_key_here"
ALPACA_SECRET_KEY="your_paper_secret_here"
```

Do not commit `.env` or share your keys.

### 3. Run (paper)

`config.yaml` already sets `paper_trading: true`.

```bash
python main.py
```

Startup looks like:

```text
10:50:50  Alpaca paper  equity $10,000.00  ACTIVE
10:50:50  Ready  81 stocks + 5 crypto  full scan  every 45s
10:50:50  Risk  stock 0.8% / crypto 0.6% of equity  max $4,000 / $2,000  slots unlimited  shorting=on
```

Entries and exits are one line each. A compact book snapshot prints about every five minutes. Stop with `Ctrl+C`.

Symbols and risk knobs load from `config.yaml` at startup — restart the bot after editing that file.

## Usage

| Command | Purpose |
| --- | --- |
| `python main.py` | Run the paper/live loop |
| `python scripts/report.py` | Print metrics + write `trading_data/metrics_snapshot.json` |
| `python scripts/report.py --no-alpaca` | Ledger-only report (no API) |
| `python backtest.py` | Replay the strategy on historical bars |
| `pytest tests/test_metrics.py -v` | Unit tests for Sharpe / Sortino / drawdown |

## Configuration

### Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `ALPACA_API_KEY` | Yes | Alpaca key id |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret |
| `ALPACA_PAPER_TRADING` | No | Overrides `config.yaml` (`true` / `false`) |
| `TRADING_CONFIG` | No | Path to YAML (default: `config.yaml`) |
| `TRADING_DATA_DIR` | No | Data directory (default: `trading_data`) |

### `config.yaml`

| Key | Purpose |
| --- | --- |
| `symbols.stocks` | Ticker list, `watchlist`, `top_N`, or `all` |
| `symbols.scan_batch_size` | Max stocks per loop (keep ≥ watchlist size for full coverage) |
| `symbols.crypto` | Crypto pairs (`BTC/USD`, …) |
| `risk.*` | Equity risk per trade, max notional, max positions, `allow_shorting` |
| `strategy.*` | Signal thresholds, ATR / min-stop, cooldowns |
| `paper_trading` | `true` = paper API; `false` = live (real money) |

**Live mode:** set `paper_trading: false` in `config.yaml` *or* `ALPACA_PAPER_TRADING=false`, and use **live** API keys.

## Architecture

```mermaid
flowchart LR
  A[Alpaca market data] --> B[Indicators + signals]
  B --> C[ATR sizing / stops]
  C --> D[Alpaca orders]
  D --> E[SQLite ledger]
  E --> F[Metrics report]
```

Stack: Python 3.10+, `requests` (Alpaca REST, no official SDK), `pandas` / `numpy`, `PyYAML`, `python-dotenv`, SQLite, `pytest`.

## Design decisions

### Alpaca IEX bars instead of Yahoo (or SIP)
**What I chose:** Live prices and bars come from Alpaca’s market-data REST endpoints with `feed=iex` for equities (`tradingbot/broker.py`). Crypto uses Alpaca’s crypto trade/bars APIs.
**Why:** An earlier loop pulled history via Yahoo/`yfinance` every scan. That was slow, rate-limited, and lagged the same venue that fills orders. Alpaca data keeps signals and execution on one broker; IEX is the free/paper-friendly equity feed.
**Trade-off:** IEX is not full SIP depth or NBBO completeness — some prints and names will look different than a paid consolidated feed.

### In-process TTL cache + full watchlist scans
**What I chose:** A small `_TTLCache` on bars/prices/account/positions (`broker.py`). Default universe is a curated priority watchlist (`stocks: watchlist`) scanned **every** loop when it fits under `scan_batch_size`. Multi-symbol bar requests use 50-symbol chunks.
**Why:** A rotating `top_500` book sorted A→Z among equal scores spent loops on obscure tickers. Caching + a ≤100-name watchlist gets liquid names every cycle without hammering Alpaca; rotation only kicks in for huge `top_N` / `all` books.
**Trade-off:** Cached closes can be up to ~`bars_ttl_seconds` stale. You only trade names on the watchlist unless you enlarge it or opt into `top_N`.

### ATR stops and size-from-risk, not fixed %
**What I chose:** Stops are `max(atr_stop_mult × ATR, min_stop_pct × price)` below longs / above shorts (`strategy._atr_stop`). Position size is `equity × risk_per_trade × conviction / |entry − stop|` with a per-trade max notional (`sizing.quantity_for_risk`). Conviction scales with score above `buy_score_min` (up to 1.3×). Trailing uses the same ATR once price has moved `trail_arm_atr_mult` in favor.
**Why:** Fixed 2–3% stops treat quiet and volatile names the same. ATR ties stop distance (and therefore share count) to recent range so risk per trade is closer to a constant dollar/R target. A minimum stop width keeps 5-minute noise from producing 0.2% stops that slippage turns into multi-R losses.
**Trade-off:** ATR is lagging and can widen in chaos, so size shrinks when you might most want conviction. There is no hard position-count cap by default (`max_positions: 0`); deployment is gated by per-trade max notional and available equity.

### Score + “strong signal” gate instead of single-indicator or full AND
**What I chose:** Entries accumulate weighted points (RSI / MACD / SMA / volume) and only fire when `buy_score_min` is met **and** at least one “strong” event fired (oversold/overbought, MACD cross, or SMA cross) — see `SignalStrategy.should_buy` / `should_short`. Soft confirmations alone never open a trade.
**Why:** Pure single-indicator entries churned; requiring every indicator at once almost never fired. Scoring with a mandatory strong leg was the middle path (also: skip longs below SMA-20 unless RSI is actually oversold).
**Trade-off:** Weights and thresholds are hand-tuned, not fit out-of-sample. Validate with paper trading and `backtest.py`, not a walk-forward optimizer (none ships in this repo).

### Dual market rules: session-gated stocks vs 24/7 crypto fills
**What I chose:** Equities only open/close when Alpaca’s clock says the session is open (`market_hours.py` / bot guards), with `time_in_force=day` and integer `qty`. Crypto is always “open,” uses `gtc` + **notional** market buys, then `wait_for_order_fill` to rewrite `order.quantity` from `filled_qty`. Crypto sells use exact qty so the bot never oversells.
**Why:** US cash equities and Alpaca spot crypto are different products — hours, order semantics, and fractional sizing don’t share one path.
**Trade-off:** Stock path does not wait on fills the same way (assumes day-market qty fills cleanly). Crypto shorts are disabled. Holiday fallback without Alpaca calendar is a short local holiday list.

### SQLite ledger with Alpaca as position source of truth
**What I chose:** Trades, bot counters, and trail events live in local SQLite (`tradingbot/memory.py`). On startup, `_restore_state` intersects open ledger rows with live Alpaca positions; missing broker positions are closed as ghosts at flat P&L.
**Why:** SQLite keeps a queryable history for `scripts/report.py` without standing up Postgres. The broker still owns what you actually hold — the ledger must not invent positions after a crash or a paper-account reset.
**Trade-off:** Single-writer, local-disk, not multi-instance safe. Ghost closes at `pnl=0` can hide real P&L if you flattened outside the bot. Metrics blend this ledger with Alpaca portfolio history when available.

## Core concepts

- **Session model:** Stocks trade only in the regular US equity session (Alpaca clock, local/holiday fallback). Crypto is treated as always open.
- **Risk unit:** Size so that a full stop ≈ a fixed fraction of **equity** (`risk.*_per_trade`), not buying power. The ledger stores `initial_risk` / `r_multiple` for post-trade review.
- **Universe:** Default is a curated priority watchlist (`stocks: watchlist` / `tradingbot/universe.py`), scanned every loop. `top_N` is priority-biased (not market cap); only rotates when larger than `scan_batch_size`.
- **Metrics:** Risk-free rate `0%`; annualize equity √252, crypto √365, blended √365 (`metrics.py`). Sharpe/Sortino are marked unreliable until ≥30 calendar days and ≥21 daily return observations.

## Project layout

```text
tradingbottest/
├── main.py                 # paper/live loop
├── backtest.py             # historical replay CLI
├── config.yaml             # symbols + risk + strategy
├── requirements.txt
├── .env.example
├── tradingbot/             # package
│   ├── bot.py              # scan / size / execute / trail
│   ├── broker.py           # Alpaca REST + cache
│   ├── strategy.py         # signal scoring + ATR stops
│   ├── sizing.py           # qty from equity risk × conviction
│   ├── universe.py         # priority watchlist
│   ├── indicators.py
│   ├── market_hours.py
│   ├── memory.py           # SQLite ledger
│   ├── metrics.py
│   ├── models.py
│   ├── config.py
│   ├── backtest.py
│   └── env.py
├── scripts/
│   └── report.py
└── tests/
    └── test_metrics.py
```

`trading_data/` is created at runtime and gitignored.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Missing ALPACA_API_KEY` | Copy `.env.example` to `.env`, run from repo root |
| `401 unauthorized` | New paper keys; keep values in double quotes |
| Could not reach Alpaca | Wrong keys, or live keys with paper mode on |
| Account status is not `ACTIVE` | Reset/create a paper account in the Alpaca dashboard, then update `.env` |
| `ModuleNotFoundError` | Activate venv, then `pip install -r requirements.txt` |
| YAML ticker becomes `True` | Quote booleans like `"ON"` in `config.yaml` |
| Metrics all `n/a` | No closed trades yet — expected early on |

## Limitations

- No published performance numbers until paper trades accumulate (`scripts/report.py`)
- Equity long + short (easy-to-borrow); crypto long-only; no portfolio optimizer
- Stock bars use Alpaca `feed=iex` (paper-friendly; SIP may need a paid data plan)
- Experimental software — paper trade first; live trading is at your own risk
