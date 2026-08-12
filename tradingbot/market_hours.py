"""Equity session checks via Alpaca clock/calendar, with a local fallback."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

import pytz

if TYPE_CHECKING:
    from tradingbot.broker import AlpacaBroker


class MarketHours:
    def __init__(self, broker: Optional["AlpacaBroker"] = None):
        self.broker = broker
        self.eastern = pytz.timezone("America/New_York")
        self._closed_dates: set[str] = set()
        self._calendar_loaded_on: str | None = None

    def is_stock_market_open(self) -> bool:
        if self.broker:
            clock = self.broker.get_clock()
            if clock and "is_open" in clock:
                return bool(clock["is_open"])

        now = datetime.now(self.eastern)
        if now.weekday() >= 5 or self._is_market_holiday(now):
            return False

        open_at = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_at = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_at <= now <= close_at

    def _refresh_calendar(self) -> None:
        if not self.broker:
            return
        today = datetime.now(self.eastern).strftime("%Y-%m-%d")
        if self._calendar_loaded_on == today:
            return

        start = (datetime.now(self.eastern) - timedelta(days=5)).strftime("%Y-%m-%d")
        end = (datetime.now(self.eastern) + timedelta(days=30)).strftime("%Y-%m-%d")
        sessions = self.broker.get_calendar(start, end)
        open_dates = {s.get("date") for s in sessions if s.get("date")}

        # Weekdays missing from Alpaca's calendar are holidays/closures.
        closed: set[str] = set()
        cursor = datetime.now(self.eastern).date() - timedelta(days=5)
        last = datetime.now(self.eastern).date() + timedelta(days=30)
        while cursor <= last:
            date_str = cursor.isoformat()
            if cursor.weekday() < 5 and date_str not in open_dates:
                closed.add(date_str)
            cursor += timedelta(days=1)

        self._closed_dates = closed
        self._calendar_loaded_on = today

    def _is_market_holiday(self, eastern_time: datetime) -> bool:
        self._refresh_calendar()
        date_str = eastern_time.strftime("%Y-%m-%d")
        if self._closed_dates:
            return date_str in self._closed_dates

        # Thin local fallback when the calendar API is unavailable.
        year, month, day = eastern_time.year, eastern_time.month, eastern_time.day
        if month == 11:
            first_day = datetime(year, 11, 1)
            first_thursday = 1 + (3 - first_day.weekday()) % 7
            if day == first_thursday + 21:
                return True
        return (month, day) in {(1, 1), (7, 4), (12, 25)}
