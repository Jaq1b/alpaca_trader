"""SQLite trade ledger, bot state, and structured trade events."""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tradingbot.models import Trade

logger = logging.getLogger(__name__)


class TradeMemory:
    """SQLite trade ledger, bot counters, and structured trade events."""

    def __init__(self, data_dir: str = "trading_data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "trading.db"
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self._maybe_migrate_json()
        logger.debug("ledger %s", self.db_path)

    def _init_db(self):
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry_price REAL,
                quantity REAL,
                timestamp TEXT,
                stop_loss REAL,
                asset_class TEXT,
                alpaca_order_id TEXT,
                exit_price REAL,
                exit_timestamp TEXT,
                status TEXT,
                pnl REAL,
                pnl_pct REAL,
                exit_reason TEXT,
                strategy_signals TEXT,
                actual_quantity REAL,
                initial_risk REAL,
                atr_at_entry REAL,
                r_multiple REAL
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_pnl REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                initial_capital REAL DEFAULT 1000,
                peak_portfolio_value REAL DEFAULT 1000,
                max_drawdown REAL DEFAULT 0,
                last_update TEXT
            );

            CREATE TABLE IF NOT EXISTS trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                event_type TEXT NOT NULL,
                symbol TEXT,
                timestamp TEXT NOT NULL,
                price REAL,
                quantity REAL,
                stop_loss REAL,
                reason TEXT,
                pnl REAL,
                pnl_pct REAL,
                r_multiple REAL,
                details TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events(trade_id);
            """)
        self._ensure_columns()
        cur.execute("SELECT COUNT(*) AS c FROM bot_state")
        if cur.fetchone()["c"] == 0:
            cur.execute(
                """
                INSERT INTO bot_state (
                    id, total_pnl, total_trades, winning_trades,
                    initial_capital, peak_portfolio_value, max_drawdown, last_update
                ) VALUES (1, 0, 0, 0, 1000, 1000, 0, ?)
                """,
                (datetime.now().isoformat(),),
            )
        self._conn.commit()

    def _ensure_columns(self):
        # Additive migrations for fields introduced after the first schema.
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(trades)")
        cols = {row[1] for row in cur.fetchall()}
        if "fees" not in cols:
            cur.execute("ALTER TABLE trades ADD COLUMN fees REAL DEFAULT 0")
        if "side" not in cols:
            cur.execute("ALTER TABLE trades ADD COLUMN side TEXT DEFAULT 'long'")
        self._conn.commit()

    def _maybe_migrate_json(self):
        trades_json = self.data_dir / "trades.json"
        if not trades_json.exists():
            return
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM trades")
        if cur.fetchone()["c"] > 0:
            return
        try:
            with open(trades_json) as f:
                rows = json.load(f)
            for row in rows:
                trade = Trade.from_dict(row)
                trade_id = row.get("trade_id") or f"{trade.symbol}_{int(time.time())}"
                self._upsert_trade(trade, trade_id)
            state_json = self.data_dir / "bot_state.json"
            if state_json.exists():
                with open(state_json) as f:
                    state = json.load(f)
                self.update_bot_state(
                    **{
                        k: v
                        for k, v in state.items()
                        if k
                        in {
                            "total_pnl",
                            "total_trades",
                            "winning_trades",
                            "initial_capital",
                            "peak_portfolio_value",
                            "max_drawdown",
                        }
                    }
                )
            self._conn.commit()
            logger.info(f"Migrated {len(rows)} trades from trades.json -> SQLite")
        except Exception as e:
            logger.error(f"JSON migration failed: {e}")

    def _upsert_trade(self, trade: Trade, trade_id: str):
        data = trade.to_dict()
        cols = [
            "trade_id",
            "symbol",
            "entry_price",
            "quantity",
            "timestamp",
            "stop_loss",
            "asset_class",
            "alpaca_order_id",
            "exit_price",
            "exit_timestamp",
            "status",
            "pnl",
            "pnl_pct",
            "exit_reason",
            "strategy_signals",
            "actual_quantity",
            "initial_risk",
            "atr_at_entry",
            "r_multiple",
            "side",
            "fees",
        ]
        values = [trade_id] + [data.get(c) for c in cols[1:]]
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        self._conn.execute(
            f"""
            INSERT INTO trades ({",".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(trade_id) DO UPDATE SET {updates}
            """,
            values,
        )

    def _row_to_trade(self, row: sqlite3.Row) -> Trade:
        return Trade.from_dict(dict(row))

    def save_trade(self, trade: Trade) -> str:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM trades")
        count = cur.fetchone()["c"]
        trade_id = f"{trade.symbol}_{int(time.time())}_{count}"
        self._upsert_trade(trade, trade_id)
        self._conn.commit()
        self.log_event(
            trade_id=trade_id,
            event_type="entry",
            symbol=trade.symbol,
            price=trade.entry_price,
            quantity=trade.quantity,
            stop_loss=trade.stop_loss,
            reason=trade.strategy_signals,
            details={
                "asset_class": trade.asset_class,
                "atr_at_entry": trade.atr_at_entry,
                "initial_risk": trade.initial_risk,
            },
        )
        return trade_id

    def update_trade(self, trade: Trade, trade_id: str):
        self._upsert_trade(trade, trade_id)
        self._conn.commit()
        if trade.status == "closed":
            self.log_event(
                trade_id=trade_id,
                event_type="exit",
                symbol=trade.symbol,
                price=trade.exit_price,
                quantity=trade.quantity,
                stop_loss=trade.stop_loss,
                reason=trade.exit_reason,
                pnl=trade.pnl,
                pnl_pct=trade.pnl_pct,
                r_multiple=trade.r_multiple,
                details={"asset_class": trade.asset_class},
            )

    def log_event(
        self,
        event_type: str,
        symbol: str,
        trade_id: str | None = None,
        price: float | None = None,
        quantity: float | None = None,
        stop_loss: float | None = None,
        reason: str | None = None,
        pnl: float | None = None,
        pnl_pct: float | None = None,
        r_multiple: float | None = None,
        details: dict[str, Any] | None = None,
    ):
        self._conn.execute(
            """
            INSERT INTO trade_events (
                trade_id, event_type, symbol, timestamp, price, quantity,
                stop_loss, reason, pnl, pnl_pct, r_multiple, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                event_type,
                symbol,
                datetime.now().isoformat(),
                price,
                quantity,
                stop_loss,
                reason,
                pnl,
                pnl_pct,
                r_multiple,
                json.dumps(details or {}),
            ),
        )
        self._conn.commit()
        logger.debug(
            "event %s %s price=%s pnl=%s R=%s %s",
            event_type,
            symbol,
            price,
            pnl,
            r_multiple,
            reason,
        )

    def get_open_trades(self) -> dict[str, tuple[Trade, str]]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM trades WHERE status = 'open'")
        open_trades = {}
        for row in cur.fetchall():
            trade = self._row_to_trade(row)
            open_trades[trade.symbol] = (trade, row["trade_id"])
        return open_trades

    def get_closed_trades(self, days: int | None = None) -> list[Trade]:
        """Closed trades only. Pass days=None for full ledger history."""
        cur = self._conn.cursor()
        if days is None:
            cur.execute(
                "SELECT * FROM trades WHERE status = 'closed' ORDER BY timestamp ASC"
            )
        else:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            cur.execute(
                """
                SELECT * FROM trades
                WHERE status = 'closed' AND substr(timestamp, 1, 10) >= ?
                ORDER BY timestamp ASC
                """,
                (cutoff,),
            )
        return [self._row_to_trade(row) for row in cur.fetchall()]

    def get_bot_state(self) -> dict:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM bot_state WHERE id = 1")
        row = cur.fetchone()
        return dict(row) if row else {}

    def update_bot_state(self, **kwargs):
        if not kwargs:
            return
        allowed = {
            "total_pnl",
            "total_trades",
            "winning_trades",
            "initial_capital",
            "peak_portfolio_value",
            "max_drawdown",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["last_update"] = datetime.now().isoformat()
        sets = ", ".join(f"{k}=?" for k in updates)
        self._conn.execute(
            f"UPDATE bot_state SET {sets} WHERE id = 1",
            list(updates.values()),
        )
        self._conn.commit()

    def get_recent_performance(self, symbol: str, days: int = 7) -> list[Trade]:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT * FROM trades
            WHERE symbol = ? AND timestamp >= ? AND status != 'open'
            ORDER BY timestamp DESC LIMIT 10
            """,
            (symbol, since),
        )
        return [self._row_to_trade(row) for row in cur.fetchall()]
