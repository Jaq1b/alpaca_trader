# alpaca_trader

Risk-first Alpaca trading bot: ATR-based sizing, broker-reconciled SQLite ledger, backtesting and performance reporting.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Paper trading](https://img.shields.io/badge/default-paper%20trading-brightgreen.svg)](https://app.alpaca.markets/paper/dashboard/overview)
[![Tests](https://github.com/Jaq1b/alpaca_trader/actions/workflows/tests.yml/badge.svg)](https://github.com/Jaq1b/alpaca_trader/actions/workflows/tests.yml)

On every start the bot reads live Alpaca positions and lines the SQLite ledger up with them. A ledger row the broker does not hold is closed at flat P&L. A position that exists only at Alpaca is recorded from the broker. Each new order is sized from the stop: account equity times a risk fraction times conviction, divided by the distance to an ATR stop. That stop is at least 0.8% of price for stocks and 1.5% for crypto, and at most 5% for stocks and 10% for crypto. The performance report prints closed P&L, win rate, and profit factor on their own, and marks Sharpe and Sortino unreliable until the sample covers 30 calendar days and 21 daily returns.

Paper trading is the default. Live mode uses real money.

## Results

**Backtest**, run 5 October 2026. Window 2025-10-01 to 2026-09-30, 5-minute bars, $10,000 starting cash. 86 symbols, 2,138,548 bars: the 81-name watchlist plus BTC, ETH, SOL, XRP, and DOGE.

| Ending capital | Closed P&L | Win rate | Trades | Avg R | Max drawdown |
| --- | --- | --- | --- | --- | --- |
| $11,333.06 | +$1,333.06 | 36.3% (103 wins, 181 losses) | 284 | 0.10 | 8.0% |

**Monte Carlo** of those 284 trades. 2,000 paths, seed 1. Each path draws the same trades again with replacement. It does not create new prices.

| | p5 | p50 | p95 |
| --- | --- | --- | --- |
| Ending capital | $9,240.32 | $11,106.67 | $14,086.27 |
| Max drawdown | | 5.3% | 11.2% |

About one path in five finishes below $10,000 (20.8%). The historical path ended at $11,333.06, near the median. Fees and slippage are not modeled. A 36% win rate at +0.10R per trade is a thin result.

The live paper account is not listed here. `python main.py report` prints closed P&L and open P&L from your own ledger. `--verbose` adds Sharpe, Sortino, and drawdown, and those ratios stay labeled unreliable until the window is long enough.

## Demo

From the repository root, with keys in `.env`:

```bash
python main.py status       # account, open book, recent closes
python main.py report       # closed-trade results
python main.py backtest     # replay bars, no orders
python main.py montecarlo   # replay once, then resample those trades
python main.py run          # scan and send paper orders until Ctrl+C
```

`status`, `report`, `backtest`, and `montecarlo` do not send orders. `python main.py` with no command is `run`.

`status` during the US session prints `regular hours`. Outside that window it prints `equities closed, crypto open`. Stocks are scanned only while the equity session is open. Crypto (BTC, ETH, SOL, XRP, DOGE) still is.

A fill is one line. This is the shape of the log, not a recorded trade:

```text
10:50:57  BUY  IBM  17 @ $229.68  stop $227.84  risk $31  RSI oversold, MACD bullish cross (Score: 6)
```

## Quick start

```bash
git clone https://github.com/Jaq1b/alpaca_trader.git
cd alpaca_trader
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Put paper keys in `.env` (they usually start with `PK`):

```env
ALPACA_API_KEY="your_paper_key_here"
ALPACA_SECRET_KEY="your_paper_secret_here"
```

```bash
python main.py status
```

You should see equity, cash, and `Status ACTIVE`. Python 3.10 or newer. Keys come from the [paper dashboard](https://app.alpaca.markets/paper/dashboard/overview).

`python main.py backtest` replays the same rules on downloaded bars and does not send orders. With no dates it uses the last `lookback_days` in `config.yaml` (30). To test a stretch you choose, pass the first and last day:

```bash
python main.py backtest --start 2026-01-02 --end 2026-03-31
```

Dates are UTC calendar days. `--end` defaults to today, so `--start 2026-06-01` runs from that day through now. The same two dates can sit in `config.yaml` as `backtest.start` and `backtest.end`. Flags on the command override the file.

The download starts a week before `--start` so RSI and ATR already have bars on the first session. Trades open only inside the dates you asked for. A long window on the full watchlist takes a while, because every symbol is downloaded for that span. To try one name, set `symbols.stocks` to a short list, run the backtest, then put `watchlist` back.

`python main.py montecarlo` runs that same replay, then builds 2,000 equity curves by resampling the closed trades. It does not send orders and it does not invent new prices.

```bash
python main.py montecarlo --start 2026-01-02 --end 2026-03-31 --paths 2000 --seed 1
```

`pytest` runs the unit tests and does not call Alpaca.

Settings, environment variables, and startup errors: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Design

- **Broker is the book.** Startup keeps ledger rows that match a live Alpaca position, records broker positions missing from the ledger, and flat-closes the rest.
- **ATR sizing.** The stop is the wider of 2.5× ATR and a minimum percent (0.8% stocks, 1.5% crypto), and is capped at 5% of price for stocks and 10% for crypto. Names quieter than that minimum are skipped. After +1R the stop moves to entry. After +1.5R it trails 1R behind price. Quantity targets a fixed fraction of equity if that stop is hit, with a dollar cap.
- **Honest ratios.** Closed P&L and open P&L are separate lines. Sharpe and Sortino are marked unreliable until 30 calendar days and 21 daily returns.
- **One venue.** Bars and orders both come from Alpaca. Equities use IEX and the regular session. Crypto is long-only and trades around the clock.
- **Score plus a strong signal.** Points from RSI, MACD, moving averages, and volume are not enough on their own. An entry also needs an RSI extreme, a MACD cross, or a moving-average cross.

Longer notes, each with what was chosen and the trade-off: [docs/DESIGN.md](docs/DESIGN.md).

## Layout

```text
main.py        status, report, backtest, montecarlo, and run
tradingbot/    broker, strategy, sizing, replay, and Monte Carlo
tests/         pytest, with no Alpaca calls
docs/          configuration and design notes
config.yaml    risk, watchlist, and the backtest window
```

## Limitations

- The year replay above is +13.3% at +0.10R per trade. In the Monte Carlo, 20.8% of reshuffles of those same trades finish below the starting $10,000.
- Fees and slippage are not modeled in the backtest.
- Equity bars use Alpaca's IEX feed, which covers only a slice of total market volume, so volume signals reflect IEX prints only.
- The stock watchlist is the current 81 names, so a backtest never sees companies that left that list.
- Crypto is long-only. Position sizing is per trade, with no portfolio-level optimizer.
- Experimental software. Paper trade first; live trading is at your own risk.

MIT license. The import package is `tradingbot/`.