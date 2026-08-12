"""Domain models for trades and broker orders."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from enum import Enum


class OrderType(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending_new"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class Trade:
    symbol: str
    entry_price: float
    quantity: float
    timestamp: str
    stop_loss: float
    asset_class: str
    alpaca_order_id: str | None = None
    exit_price: float | None = None
    exit_timestamp: str | None = None
    status: str = "open"
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str | None = None
    strategy_signals: str | None = None
    actual_quantity: float | None = None
    initial_risk: float | None = None
    atr_at_entry: float | None = None
    r_multiple: float | None = None
    side: str = "long"
    fees: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "Trade":
        payload = data.copy()
        payload.pop("trade_id", None)
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in valid})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Order:
    symbol: str
    order_type: OrderType
    quantity: float
    price: float | None = None
    timestamp: datetime = None
    status: OrderStatus = OrderStatus.PENDING
    order_id: str | None = None
    asset_class: str = "stock"
    notional: float | None = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
