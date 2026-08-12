"""Run a historical backtest: python backtest.py"""

from tradingbot.env import load_env

load_env()

from main import run_backtest

if __name__ == "__main__":
    run_backtest()
