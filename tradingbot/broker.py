"""Alpaca REST client: trading, market data, portfolio history, TTL cache."""

import logging
import time
from decimal import ROUND_DOWN, Decimal

import pandas as pd
import requests

from tradingbot.models import Order, OrderStatus

logger = logging.getLogger(__name__)


class _TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value, ttl: float):
        self._store[key] = (time.time() + ttl, value)

    def invalidate(self, prefix: str = ""):
        if not prefix:
            self._store.clear()
            return
        for key in list(self._store):
            if key.startswith(prefix):
                self._store.pop(key, None)


class AlpacaBroker:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper_trading: bool = True,
        cache_config: dict | None = None,
    ):
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.paper_trading = paper_trading
        self.cache = _TTLCache()
        self.cache_cfg = cache_config or {
            "bars_ttl_seconds": 90,
            "price_ttl_seconds": 15,
            "account_ttl_seconds": 10,
            "positions_ttl_seconds": 10,
            "clock_ttl_seconds": 30,
            "calendar_ttl_seconds": 3600,
        }

        if paper_trading:
            self.base_url = "https://paper-api.alpaca.markets"
            logger.info("Using PAPER TRADING mode")
        else:
            self.base_url = "https://api.alpaca.markets"
            logger.warning("Using LIVE TRADING mode")

        self.data_url = "https://data.alpaca.markets"
        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        self._test_connection()

    def _is_crypto(self, symbol: str) -> bool:
        return "/" in symbol

    def _get(
        self, url: str, params: dict | None = None, timeout: int = 15
    ) -> dict | list:
        response = requests.get(
            url, headers=self.headers, params=params, timeout=timeout
        )
        if response.status_code != 200:
            logger.error(
                f"GET {url} failed: {response.status_code} - {response.text[:200]}"
            )
            response.raise_for_status()
        return response.json()

    def get_actual_position_quantity(self, symbol: str) -> float:
        """Return signed qty (negative = short). Crypto is long-only and floored."""
        try:
            positions = self.get_positions()
            for pos in positions:
                if pos["symbol"] == symbol:
                    qty = float(pos["qty"])
                    if self._is_crypto(symbol) and qty > 0:
                        if qty < 1:
                            return float(
                                Decimal(str(qty)).quantize(
                                    Decimal("0.00000001"), rounding=ROUND_DOWN
                                )
                            )
                        return float(
                            Decimal(str(qty)).quantize(
                                Decimal("0.00001"), rounding=ROUND_DOWN
                            )
                        )
                    return qty
            return 0.0
        except Exception as e:
            logger.error(f"Error getting actual position for {symbol}: {e}")
            return 0.0

    def is_shortable(self, symbol: str) -> bool:
        """True when Alpaca marks the equity shortable and easy to borrow."""
        if self._is_crypto(symbol):
            return False
        cache_key = f"asset:{symbol}"
        cached = self.cache.get(cache_key)
        if cached is None:
            try:
                cached = self._get(f"{self.base_url}/v2/assets/{symbol}")
                if not isinstance(cached, dict):
                    return False
                self.cache.set(cache_key, cached, 3600)
            except Exception as e:
                logger.debug(f"Asset lookup failed for {symbol}: {e}")
                return False
        return bool(cached.get("shortable")) and bool(cached.get("easy_to_borrow"))

    def _test_connection(self):
        try:
            account = self.get_account(force=True)
            if account and "account_number" in account:
                logger.info("Connected to Alpaca successfully")
                equity = float(
                    account.get("equity") or account.get("portfolio_value") or 0
                )
                buying_power = float(account.get("buying_power") or 0)
                logger.info(
                    f"Equity: ${equity:,.2f} | Buying power: ${buying_power:,.2f} "
                    f"(buying power includes margin — often ~4x equity on paper)"
                )
                return True
            logger.error("Account data missing or invalid")
            return False
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False

    def get_account(self, force: bool = False) -> dict:
        if not force:
            cached = self.cache.get("account")
            if cached is not None:
                return cached
        try:
            data = self._get(f"{self.base_url}/v2/account")
            self.cache.set("account", data, self.cache_cfg["account_ttl_seconds"])
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting account info: {e}")
            return {}

    def get_clock(self, force: bool = False) -> dict:
        if not force:
            cached = self.cache.get("clock")
            if cached is not None:
                return cached
        try:
            data = self._get(f"{self.base_url}/v2/clock")
            self.cache.set("clock", data, self.cache_cfg["clock_ttl_seconds"])
            return data
        except Exception as e:
            logger.error(f"Error getting clock: {e}")
            return {}

    def get_calendar(self, start: str, end: str) -> list[dict]:
        key = f"calendar:{start}:{end}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            data = self._get(
                f"{self.base_url}/v2/calendar",
                params={"start": start, "end": end},
            )
            self.cache.set(key, data, self.cache_cfg["calendar_ttl_seconds"])
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Error getting calendar: {e}")
            return []

    def get_current_price(self, symbol: str) -> float:
        cached = self.cache.get(f"price:{symbol}")
        if cached is not None:
            return cached

        # Prefer last bar close from cache to avoid extra round-trips
        bars_cached = self.cache.get(f"bars:{symbol}")
        if bars_cached is not None and not bars_cached.empty:
            price = float(bars_cached.iloc[-1]["Close"])
            self.cache.set(
                f"price:{symbol}", price, self.cache_cfg["price_ttl_seconds"]
            )
            return price

        try:
            if self._is_crypto(symbol):
                data = self._get(
                    f"{self.data_url}/v1beta3/crypto/us/latest/trades",
                    params={"symbols": symbol},
                )
                trade = data.get("trades", {}).get(symbol, {})
                price = float(trade.get("p", 0) or 0)
            else:
                data = self._get(
                    f"{self.data_url}/v2/stocks/{symbol}/trades/latest",
                    params={"feed": "iex"},
                )
                price = float(data.get("trade", {}).get("p", 0) or 0)

            if price > 0:
                self.cache.set(
                    f"price:{symbol}", price, self.cache_cfg["price_ttl_seconds"]
                )
            return price
        except Exception as e:
            logger.debug(f"Price lookup failed for {symbol}: {e}")
            return 0.0

    def _bars_to_df(self, bars: list[dict]) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        # Alpaca bar fields are t/o/h/l/c/v
        rename = {
            "t": "timestamp",
            "o": "Open",
            "h": "High",
            "l": "Low",
            "c": "Close",
            "v": "Volume",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.set_index("timestamp")
        keep = [
            c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns
        ]
        return df[keep].astype(float)

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "5Min",
        limit: int = 100,
        start: str | None = None,
        end: str | None = None,
        force: bool = False,
    ) -> pd.DataFrame:
        cache_key = f"bars:{symbol}:{timeframe}:{limit}:{start}:{end}"
        if not force:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
            # shorter key used by price shortcut
            short = self.cache.get(f"bars:{symbol}")
            if short is not None and start is None and end is None:
                return short.tail(limit) if len(short) > limit else short

        try:
            if self._is_crypto(symbol):
                params = {
                    "symbols": symbol,
                    "timeframe": timeframe,
                    "limit": min(limit, 10000),
                }
                if start:
                    params["start"] = start
                if end:
                    params["end"] = end
                data = self._get(
                    f"{self.data_url}/v1beta3/crypto/us/bars",
                    params=params,
                )
                bars = data.get("bars", {}).get(symbol, [])
            else:
                params = {
                    "timeframe": timeframe,
                    "limit": min(limit, 10000),
                    "adjustment": "raw",
                    "feed": "iex",
                }
                if start:
                    params["start"] = start
                if end:
                    params["end"] = end
                data = self._get(
                    f"{self.data_url}/v2/stocks/{symbol}/bars",
                    params=params,
                )
                bars = data.get("bars", []) or []

            df = self._bars_to_df(bars)
            if not df.empty:
                self.cache.set(cache_key, df, self.cache_cfg["bars_ttl_seconds"])
                if start is None and end is None:
                    self.cache.set(
                        f"bars:{symbol}", df, self.cache_cfg["bars_ttl_seconds"]
                    )
                    last = float(df.iloc[-1]["Close"])
                    self.cache.set(
                        f"price:{symbol}", last, self.cache_cfg["price_ttl_seconds"]
                    )
            return df
        except Exception as e:
            logger.debug(f"Bars failed for {symbol}: {e}")
            return pd.DataFrame()

    def get_bars_batch(
        self,
        symbols: list[str],
        timeframe: str = "5Min",
        limit: int = 100,
    ) -> dict[str, pd.DataFrame]:
        """Fetch bars for many symbols, using cache and batching where possible."""
        result: dict[str, pd.DataFrame] = {}
        stocks = [s for s in symbols if not self._is_crypto(s)]
        cryptos = [s for s in symbols if self._is_crypto(s)]

        missing_stocks = []
        for symbol in stocks:
            cached = self.cache.get(f"bars:{symbol}")
            if cached is not None:
                result[symbol] = cached.tail(limit)
            else:
                missing_stocks.append(symbol)

        if missing_stocks:
            chunk_size = 50
            for i in range(0, len(missing_stocks), chunk_size):
                chunk = missing_stocks[i : i + chunk_size]
                try:
                    data = self._get(
                        f"{self.data_url}/v2/stocks/bars",
                        params={
                            "symbols": ",".join(chunk),
                            "timeframe": timeframe,
                            "limit": limit,
                            "adjustment": "raw",
                            "feed": "iex",
                        },
                    )
                    bars_map = data.get("bars", {}) or {}
                    for symbol in chunk:
                        df = self._bars_to_df(bars_map.get(symbol, []))
                        if not df.empty:
                            self.cache.set(
                                f"bars:{symbol}",
                                df,
                                self.cache_cfg["bars_ttl_seconds"],
                            )
                            self.cache.set(
                                f"price:{symbol}",
                                float(df.iloc[-1]["Close"]),
                                self.cache_cfg["price_ttl_seconds"],
                            )
                        result[symbol] = df
                except Exception as e:
                    logger.debug(f"Batch stock bars failed for {chunk}: {e}")
                    for symbol in chunk:
                        result[symbol] = self.get_bars(symbol, timeframe, limit)

        for symbol in cryptos:
            result[symbol] = self.get_bars(symbol, timeframe, limit)

        return result

    def get_order_details(self, order_id: str) -> dict:
        try:
            return self._get(f"{self.base_url}/v2/orders/{order_id}")
        except Exception as e:
            logger.error(f"Error getting order details: {e}")
            return {}

    def wait_for_order_fill(self, order_id: str, timeout: int = 30) -> dict:
        start_time = time.time()
        while time.time() - start_time < timeout:
            order_details = self.get_order_details(order_id)
            if order_details:
                status = order_details.get("status", "")
                if status in ["filled", "partially_filled", "cancelled", "rejected"]:
                    return order_details
            time.sleep(1)
        return self.get_order_details(order_id)

    def place_order(self, order: Order) -> bool:
        try:
            time_in_force = "gtc" if order.asset_class == "crypto" else "day"

            if order.asset_class == "crypto":
                current_price = self.get_current_price(order.symbol)
                if current_price <= 0:
                    logger.error(f"Cannot get price for {order.symbol}")
                    return False

                notional = (
                    order.notional if order.notional else order.quantity * current_price
                )
                if notional < 1.0:
                    logger.error(f"Notional value too small: ${notional:.2f}")
                    return False

                order_data = {
                    "symbol": order.symbol,
                    "notional": str(round(notional, 2)),
                    "side": order.order_type.value,
                    "type": "market",
                    "time_in_force": time_in_force,
                }
            else:
                order_data = {
                    "symbol": order.symbol,
                    "qty": str(int(order.quantity)),
                    "side": order.order_type.value,
                    "type": "market",
                    "time_in_force": time_in_force,
                }

            logger.info(
                f"Placing {order.order_type.value.upper()} order for {order.symbol}: {order_data}"
            )
            response = requests.post(
                f"{self.base_url}/v2/orders",
                headers=self.headers,
                json=order_data,
                timeout=10,
            )

            if response.status_code in [200, 201]:
                result = response.json()
                order.order_id = result["id"]
                order.status = OrderStatus.PENDING
                logger.info(f"Order placed successfully: {order.order_id}")
                self.cache.invalidate("positions")
                self.cache.invalidate("account")

                if order.asset_class == "crypto":
                    fill_details = self.wait_for_order_fill(order.order_id, timeout=15)
                    if fill_details and fill_details.get("status") == "filled":
                        filled_qty = float(fill_details.get("filled_qty", 0))
                        if filled_qty > 0:
                            order.quantity = filled_qty
                            logger.info(
                                f"Crypto order filled: {filled_qty} {order.symbol}"
                            )
                return True

            logger.error(f"Order failed: {response.status_code} - {response.text}")
            return False
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return False

    def get_positions(self, force: bool = False) -> list[dict]:
        if not force:
            cached = self.cache.get("positions")
            if cached is not None:
                return cached
        try:
            data = self._get(f"{self.base_url}/v2/positions")
            positions = data if isinstance(data, list) else []
            self.cache.set(
                "positions", positions, self.cache_cfg["positions_ttl_seconds"]
            )
            return positions
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    def get_portfolio_history(
        self,
        period: str = "3M",
        timeframe: str = "1D",
        force: bool = False,
    ) -> dict:
        """
        Alpaca account equity curve for MTM daily returns.

        Returns dict with timestamp, equity, profit_loss, profit_loss_pct, base_value.
        """
        key = f"portfolio_history:{period}:{timeframe}"
        if not force:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        try:
            data = self._get(
                f"{self.base_url}/v2/account/portfolio/history",
                params={
                    "period": period,
                    "timeframe": timeframe,
                    "extended_hours": "true",
                },
            )
            self.cache.set(key, data, 60)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Error getting portfolio history: {e}")
            return {}

    def get_equity_universe(
        self,
        exclude_otc: bool = True,
        tradable_only: bool = True,
        limit: int | None = None,
    ) -> list[str]:
        """
        Active US equities on Alpaca.

        When limit is set (e.g. 1000), keeps the most liquid-looking names first:
        fractionable / easy_to_borrow / marginable on major exchanges, then
        fills up to `limit`. Not a true market-cap ranking (Alpaca assets API
        doesn't expose that), but a practical "top N tradable" book.
        """
        cache_key = f"equity_universe:{exclude_otc}:{tradable_only}:{limit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            assets = self._get(
                f"{self.base_url}/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                timeout=60,
            )
            if not isinstance(assets, list):
                logger.error("Unexpected assets payload from Alpaca")
                return []

            otc_exchanges = {"OTC", "OTCM", "OTCQB", "OTCQX", "PINK"}
            major_exchanges = {
                "NYSE",
                "NASDAQ",
                "ARCA",
                "AMEX",
                "BATS",
                "NYSEARCA",
                "NYSEAMERICAN",
            }
            from tradingbot.universe import PRIORITY_SET, PRIORITY_SYMBOLS

            priority_rank = {sym: i for i, sym in enumerate(PRIORITY_SYMBOLS)}
            ranked: list[tuple] = []
            for asset in assets:
                symbol = str(asset.get("symbol") or "").strip().upper()
                if not symbol or not symbol.replace(".", "").isalnum():
                    continue
                if tradable_only and not asset.get("tradable", False):
                    continue
                exchange = str(asset.get("exchange") or "").upper()
                if exclude_otc and exchange in otc_exchanges:
                    continue

                # Names people care about outrank alphabetical ties among
                # equally "liquid-looking" Alpaca flags.
                score = 0
                if symbol in PRIORITY_SET:
                    score += 10_000 - priority_rank[symbol]
                if exchange in major_exchanges:
                    score += 4
                if asset.get("fractionable"):
                    score += 3
                if asset.get("easy_to_borrow"):
                    score += 2
                if asset.get("marginable"):
                    score += 1
                if "." not in symbol and len(symbol) <= 4:
                    score += 1
                ranked.append((score, symbol))

            # Score desc only — do not secondary-sort A→Z (that buried NVDA)
            ranked.sort(key=lambda row: -row[0])
            symbols = [sym for _, sym in ranked]
            if limit is not None and limit > 0:
                symbols = symbols[:limit]

            self.cache.set(cache_key, symbols, 3600)
            logger.info(
                f"Loaded {len(symbols)} US equities from Alpaca"
                + (f" (top {limit}, priority-biased)" if limit else "")
            )
            return symbols
        except Exception as e:
            logger.error(f"Error loading equity universe: {e}")
            return []
