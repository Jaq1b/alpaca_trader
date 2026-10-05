"""Layout checks for the status table."""

from tradingbot.display import PositionView, format_positions, signed_money
from tradingbot.metrics import format_summary


def test_position_table_aligns_columns():
    text = format_positions(
        [
            PositionView(
                symbol="IBM",
                side="long",
                qty=17,
                entry=229.68,
                last=230.10,
                pnl=7.14,
                pnl_pct=0.2,
                stop=227.84,
            )
        ]
    )
    lines = text.splitlines()
    assert "Symbol" in lines[0]
    assert "IBM" in lines[1]
    assert "+$7.14" in lines[1]
    assert "227.84" in lines[1]


def test_empty_book():
    assert "No open positions" in format_positions([])


def test_signed_money():
    assert signed_money(-12.5) == "-$12.50"
    assert signed_money(3) == "+$3.00"


def test_summary_is_short():
    text = format_summary(
        {
            "evaluation": {"start": "2026-08-01", "end": "2026-08-17"},
            "returns": {
                "absolute_pnl_closed_trades": -10.5,
                "absolute_pnl_equity": -8.0,
                "absolute_pnl_crypto": -2.5,
            },
            "trade_stats": {
                "total_trades": 4,
                "win_rate": 0.5,
                "average_win": 3.0,
                "average_loss": -8.0,
                "profit_factor": 0.4,
                "by_asset_class": {"equity": 3, "crypto": 1},
            },
            "alpaca_reconciliation": {},
        }
    )
    assert "Closed P&L" in text
    assert "Sharpe" not in text
    assert "$-10.50" in text
