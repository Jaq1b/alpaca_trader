"""Paper-trading entrypoint.

python main.py status
python main.py report
python main.py run
python main.py backtest
python main.py montecarlo
"""

from tradingbot.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
