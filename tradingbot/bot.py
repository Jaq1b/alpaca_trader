"""Main trading loop: scan, size, execute, trail stops, restore state."""

import logging
import re
import time
from datetime import datetime
from typing import Any

from tradingbot.broker import AlpacaBroker
from tradingbot.config import load_config
from tradingbot.display import PositionView, format_positions, signed_money
from tradingbot.market_hours import MarketHours
from tradingbot.memory import TradeMemory
from tradingbot.models import Order, OrderType, Trade
from tradingbot.sizing import (
    favorable_r,
    quantity_for_risk,
    tighten_stop,
    unrealized_pnl,
)
from tradingbot.strategy import SignalStrategy
from tradingbot.universe import PRIORITY_SYMBOLS

logger = logging.getLogger(__name__)


def _qty_str(qty: float, asset_class: str) -> str:
    if asset_class == "crypto":
        return f"{qty:.6g}"
    rounded = round(qty)
    if abs(qty - rounded) < 1e-6:
        return str(int(rounded))
    return f"{qty:.2f}"


def _brief_reason(reason: str) -> str:
    text = reason or ""
    for prefix in ("BUY: ", "SHORT: ", "SELL: ", "COVER: "):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


class TradingBot:
    def __init__(
        self,
        broker: AlpacaBroker,
        config: dict[str, Any] | None = None,
        data_dir: str | None = None,
    ):
        self.config = config or load_config()
        self.broker = broker
        self.initial_capital = float(self.config.get("initial_capital", 100000.0))
        self.market_hours = MarketHours(broker)
        self.memory = TradeMemory(
            data_dir or self.config.get("data_dir", "trading_data")
        )

        risk = self.config.get("risk", {})
        symbols_cfg = self.config.get("symbols", {})
        strategy_cfg = self.config.get("strategy", {})

        self.scan_batch_size = int(symbols_cfg.get("scan_batch_size", 100))
        self._scan_offset = 0

        self.stock_symbols = self._resolve_stock_symbols(symbols_cfg.get("stocks"))
        self.crypto_symbols = [
            str(s) for s in symbols_cfg.get("crypto", ["BTC/USD", "ETH/USD"])
        ]

        self.strategy = SignalStrategy(self.memory, strategy_cfg)

        self.check_interval = int(risk.get("check_interval_seconds", 45))
        # 0 = unlimited; per-trade max notional + equity gate deployment.
        self.max_positions = int(risk.get("max_positions", 0))
        self.min_hold_time = int(risk.get("min_hold_seconds", 300))
        self.crypto_risk_per_trade = float(risk.get("crypto_per_trade", 0.006))
        self.stock_risk_per_trade = float(risk.get("stock_per_trade", 0.008))
        self.max_position_value_crypto = float(
            risk.get("max_position_value_crypto", 2000.0)
        )
        self.max_position_value_stock = float(
            risk.get("max_position_value_stock", 4000.0)
        )
        self.min_position_value = float(risk.get("min_position_value", 250.0))
        self.allow_shorting = bool(risk.get("allow_shorting", True))
        self.min_bars = int(strategy_cfg.get("min_bars", 40))

        self.breakeven_r = float(strategy_cfg.get("breakeven_r", 1.0))
        self.trail_arm_r = float(strategy_cfg.get("trail_arm_r", 1.5))
        self.trail_distance_r = float(strategy_cfg.get("trail_distance_r", 1.0))

        self.active_positions: dict[str, dict[str, Any]] = {}
        self._restore_state()

        bot_state = self.memory.get_bot_state()
        self.total_pnl = bot_state.get("total_pnl", 0.0) or 0.0
        self.total_trades = bot_state.get("total_trades", 0) or 0
        self.winning_trades = bot_state.get("winning_trades", 0) or 0

        scan_mode = (
            "full scan"
            if len(self.stock_symbols) <= self.scan_batch_size
            else f"rotate {self.scan_batch_size}/loop"
        )
        slots = (
            "no position cap"
            if self.max_positions <= 0
            else f"{self.max_positions} max"
        )
        logger.info(
            f"Watching {len(self.stock_symbols)} stocks and "
            f"{len(self.crypto_symbols)} crypto, {scan_mode} every {self.check_interval}s"
        )
        logger.info(
            f"Risk {self.stock_risk_per_trade:.1%} of equity per stock, "
            f"{self.crypto_risk_per_trade:.1%} per crypto, "
            f"max ${self.max_position_value_stock:,.0f} / "
            f"${self.max_position_value_crypto:,.0f}, {slots}"
        )

    def _resolve_stock_symbols(self, stocks_cfg) -> list[str]:
        """Map config stocks setting to a ticker list."""
        if isinstance(stocks_cfg, str):
            raw = stocks_cfg.strip().lower()
            if raw == "watchlist":
                return list(PRIORITY_SYMBOLS)
            if raw == "all":
                universe = self.broker.get_equity_universe()
            else:
                match = re.fullmatch(r"top_(\d+)", raw)
                if match:
                    universe = self.broker.get_equity_universe(
                        limit=int(match.group(1))
                    )
                else:
                    logger.error(f"Unknown stocks setting {stocks_cfg!r}")
                    universe = []

            if not universe:
                logger.error("Equity universe empty; check Alpaca connectivity")
            return universe

        if isinstance(stocks_cfg, list):
            return [str(s).upper() for s in stocks_cfg]

        return list(PRIORITY_SYMBOLS)

    def _symbols_for_this_scan(self, stock_market_open: bool) -> list[str]:
        """Full watchlist each loop; rotate only when larger than scan_batch_size."""
        selected: list[str] = []

        if stock_market_open and self.stock_symbols:
            max_per = max(1, self.scan_batch_size)
            n = len(self.stock_symbols)

            if n <= max_per:
                selected.extend(self.stock_symbols)
            else:
                start = self._scan_offset % n
                end = start + max_per
                if end <= n:
                    chunk = self.stock_symbols[start:end]
                else:
                    chunk = self.stock_symbols[start:] + self.stock_symbols[: end % n]
                self._scan_offset = end % n
                selected.extend(chunk)

            for symbol in self.active_positions:
                if self._get_asset_class(symbol) == "stock" and symbol not in selected:
                    selected.append(symbol)

        selected.extend(self.crypto_symbols)
        return selected

    def _get_asset_class(self, symbol: str) -> str:
        return "crypto" if symbol in self.crypto_symbols else "stock"

    def _position_side(self, position: dict[str, Any]) -> str:
        return (
            position.get("side") or getattr(position["trade"], "side", None) or "long"
        )

    def _to_bot_symbol(self, alpaca_symbol: str) -> str:
        """Map Alpaca BTCUSD → config BTC/USD when we know the pair."""
        key = AlpacaBroker.normalize_symbol(alpaca_symbol)
        for symbol in self.crypto_symbols:
            if AlpacaBroker.normalize_symbol(symbol) == key:
                return symbol
        return str(alpaca_symbol).upper()

    def _restore_state(self):
        """
        Alpaca is the source of truth for open positions.

        - Every non-dust Alpaca position becomes an active bot position.
        - Ledger rows are attached when they match; otherwise we sync a new row.
        - Ledger "open" rows with no Alpaca position are closed locally.
        """
        memory_open = self.memory.get_open_trades()
        memory_by_key = {
            AlpacaBroker.normalize_symbol(symbol): (symbol, trade, trade_id)
            for symbol, (trade, trade_id) in memory_open.items()
        }

        alpaca_positions = self.broker.get_positions(force=True)
        self.active_positions = {}
        synced: list[str] = []
        closed_stale = 0

        seen_keys: set[str] = set()
        for alpaca_pos in alpaca_positions:
            if float(alpaca_pos.get("qty") or 0) == 0:
                continue
            if AlpacaBroker.is_dust_position(alpaca_pos):
                logger.debug(
                    "ignore dust %s qty=%s",
                    alpaca_pos.get("symbol"),
                    alpaca_pos.get("qty"),
                )
                continue

            key = AlpacaBroker.normalize_symbol(alpaca_pos["symbol"])
            seen_keys.add(key)
            symbol = self._to_bot_symbol(alpaca_pos["symbol"])
            signed_qty = float(alpaca_pos["qty"])
            qty_abs = abs(signed_qty)
            side = "short" if signed_qty < 0 else "long"
            entry = float(alpaca_pos.get("avg_entry_price") or 0) or float(
                alpaca_pos.get("current_price") or 0
            )
            if entry <= 0:
                continue

            if key in memory_by_key:
                _sym, trade, trade_id = memory_by_key[key]
                symbol = _sym  # keep config/ledger symbol (BTC/USD)
                trade.side = side
                trade.quantity = qty_abs
                trade.actual_quantity = qty_abs
                trade.entry_price = entry
                trade.status = "open"
                self.memory.update_trade(trade, trade_id)
                source = "ledger+alpaca"
            else:
                asset_class = self._get_asset_class(symbol)
                stop = entry * (0.97 if side == "long" else 1.03)
                trade = Trade(
                    symbol=symbol,
                    entry_price=entry,
                    quantity=qty_abs,
                    timestamp=datetime.now().isoformat(),
                    stop_loss=stop,
                    asset_class=asset_class,
                    status="open",
                    strategy_signals="Synced from Alpaca",
                    actual_quantity=qty_abs,
                    initial_risk=abs(entry - stop) * qty_abs,
                    atr_at_entry=0.0,
                    side=side,
                    fees=0.0,
                )
                trade_id = self.memory.save_trade(trade)
                source = "alpaca-only"

            risk_dist = abs(trade.entry_price - trade.stop_loss)
            self.active_positions[symbol] = {
                "trade": trade,
                "trade_id": trade_id,
                "quantity": qty_abs,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "timestamp": datetime.fromisoformat(trade.timestamp),
                "asset_class": trade.asset_class,
                "atr": trade.atr_at_entry or 0.0,
                "initial_risk": trade.initial_risk or risk_dist * qty_abs,
                "risk_per_unit": risk_dist,
                "side": side,
            }
            synced.append(f"{symbol}({side},{source})")

        # Ledger opens that Alpaca does not hold → close locally (Alpaca wins).
        for key, (symbol, trade, trade_id) in memory_by_key.items():
            if key in seen_keys:
                continue
            trade.exit_price = trade.entry_price
            trade.exit_timestamp = datetime.now().isoformat()
            trade.status = "closed"
            trade.pnl = 0.0
            trade.pnl_pct = 0.0
            trade.exit_reason = "Closed — not on Alpaca"
            self.memory.update_trade(trade, trade_id)
            closed_stale += 1

        logger.info(
            f"Sync  {len(synced)} open on Alpaca"
            + (f", closed {closed_stale} stale ledger row(s)" if closed_stale else "")
        )
        if synced:
            logger.info("Open  " + ", ".join(synced))

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        asset_class: str,
        score: int = 0,
    ) -> float:
        account = self.broker.get_account()
        equity = float(
            account.get("equity")
            or account.get("portfolio_value")
            or self.initial_capital
        )
        max_val = (
            self.max_position_value_crypto
            if asset_class == "crypto"
            else self.max_position_value_stock
        )
        risk_frac = (
            self.crypto_risk_per_trade
            if asset_class == "crypto"
            else self.stock_risk_per_trade
        )
        return quantity_for_risk(
            equity=equity,
            risk_per_trade=risk_frac,
            entry_price=entry_price,
            stop_loss=stop_loss,
            asset_class=asset_class,
            score=score,
            score_min=self.strategy.entry_score_min(asset_class),
            max_position_value=max_val,
            min_position_value=self.min_position_value,
        )

    def execute_open(
        self,
        symbol: str,
        current_price: float,
        stop_loss: float,
        reason: str,
        atr: float = 0.0,
        side: str = "long",
        score: int = 0,
    ):
        """Open a long (BUY) or short (SELL) stock/crypto position."""
        side = "short" if side == "short" else "long"
        if self.max_positions > 0 and len(self.active_positions) >= self.max_positions:
            return False

        asset_class = self._get_asset_class(symbol)
        if side == "short":
            if not self.allow_shorting or asset_class != "stock":
                return False
            if not self.broker.is_shortable(symbol):
                logger.debug(f"Skip short {symbol}: not shortable / hard to borrow")
                return False

        if asset_class == "stock" and not self.market_hours.is_stock_market_open():
            return False

        # Broker already holds this name (ledger miss / symbol mismatch recovery).
        existing = self.broker.find_position(symbol, force=True)
        if existing is not None and not AlpacaBroker.is_dust_position(existing):
            logger.warning(
                f"Skip {symbol} — Alpaca already holds {existing.get('qty')}"
            )
            return False

        position_size = self.calculate_position_size(
            symbol, current_price, stop_loss, asset_class, score=score
        )
        if position_size <= 0:
            return False

        order_type = OrderType.BUY if side == "long" else OrderType.SELL
        order = Order(
            symbol=symbol,
            order_type=order_type,
            quantity=position_size,
            asset_class=asset_class,
        )

        initial_risk = abs(current_price - stop_loss) * position_size
        action = "BUY" if side == "long" else "SHORT"
        logger.info(
            f"{action}  {symbol}  {_qty_str(position_size, asset_class)} "
            f"@ ${current_price:.2f}  stop ${stop_loss:.2f}  "
            f"risk ${initial_risk:.0f}  {_brief_reason(reason)}"
        )

        if self.broker.place_order(order):
            actual_quantity = abs(float(order.quantity))
            initial_risk = abs(current_price - stop_loss) * actual_quantity

            trade = Trade(
                symbol=symbol,
                entry_price=current_price,
                quantity=actual_quantity,
                timestamp=datetime.now().isoformat(),
                stop_loss=stop_loss,
                asset_class=asset_class,
                alpaca_order_id=order.order_id,
                status="open",
                strategy_signals=reason,
                actual_quantity=actual_quantity,
                initial_risk=initial_risk,
                atr_at_entry=atr,
                side=side,
                fees=0.0,
            )

            trade_id = self.memory.save_trade(trade)
            self.active_positions[symbol] = {
                "trade": trade,
                "trade_id": trade_id,
                "quantity": actual_quantity,
                "entry_price": current_price,
                "stop_loss": stop_loss,
                "timestamp": datetime.now(),
                "asset_class": asset_class,
                "atr": atr,
                "initial_risk": initial_risk,
                "risk_per_unit": abs(current_price - stop_loss),
                "side": side,
            }
            return True

        return False

    def execute_close(self, symbol: str, reason: str):
        """Close a long (SELL) or short (BUY / cover)."""
        if symbol not in self.active_positions:
            return False

        position = self.active_positions[symbol]
        asset_class = position["asset_class"]
        side = self._position_side(position)

        if asset_class == "stock" and not self.market_hours.is_stock_market_open():
            if "stop loss" not in reason.lower():
                return False

        current_price = self.broker.get_current_price(symbol)
        if current_price <= 0:
            logger.error(f"Cannot get current price for {symbol}")
            return False

        hold_time = (datetime.now() - position["timestamp"]).total_seconds()
        if hold_time < self.min_hold_time and "stop loss" not in reason.lower():
            return False

        signed_qty = self.broker.get_actual_position_quantity(symbol)
        expected_sign = -1 if side == "short" else 1
        if signed_qty == 0 or (signed_qty > 0) != (expected_sign > 0):
            logger.warning(f"{symbol} gone on Alpaca — dropped from ledger")
            trade = position["trade"]
            trade.exit_price = current_price
            trade.exit_timestamp = datetime.now().isoformat()
            trade.status = "closed"
            trade.pnl = 0.0
            trade.pnl_pct = 0.0
            trade.exit_reason = "Position not found - cleaned up"
            self.memory.update_trade(trade, position["trade_id"])
            del self.active_positions[symbol]
            return False

        close_quantity = abs(signed_qty)
        # Closing a long = sell; covering a short = buy.
        # Crypto exits must use qty (not notional) so we never oversell.
        order_type = OrderType.SELL if side == "long" else OrderType.BUY
        order = Order(
            symbol=symbol,
            order_type=order_type,
            quantity=close_quantity,
            asset_class=asset_class,
        )

        pnl, pnl_pct = unrealized_pnl(
            position["entry_price"], current_price, close_quantity, side
        )
        initial_risk = position.get("initial_risk") or 0.0
        r_multiple = (pnl / initial_risk) if initial_risk > 0 else None

        if reason.lower() == "stop loss":
            action = "STOP"
        else:
            action = "SELL" if side == "long" else "COVER"
        r_txt = f"{r_multiple:.2f}" if r_multiple is not None else "n/a"
        logger.info(
            f"{action}  {symbol}  {_qty_str(close_quantity, asset_class)} "
            f"@ ${current_price:.2f}  P&L ${pnl:.2f} ({pnl_pct:+.1f}%)  "
            f"R={r_txt}  {_brief_reason(reason)}"
        )

        if self.broker.place_order(order):
            # Crypto fills can leave sub-cent dust; try one exact sweep, then ignore.
            if asset_class == "crypto" and side == "long":
                self.broker.cache.invalidate("positions")
                leftover = self.broker.find_position(symbol, force=True)
                if leftover and not AlpacaBroker.is_dust_position(leftover):
                    logger.warning(f"Sweep leftover {symbol} qty={leftover.get('qty')}")
                    sweep = Order(
                        symbol=symbol,
                        order_type=OrderType.SELL,
                        quantity=abs(float(leftover["qty"])),
                        asset_class="crypto",
                    )
                    self.broker.place_order(sweep)
                elif leftover and AlpacaBroker.is_dust_position(leftover):
                    logger.debug(
                        "dust left %s qty=%s",
                        leftover.get("qty"),
                        leftover.get("market_value"),
                    )

            trade = position["trade"]
            trade.exit_price = current_price
            trade.exit_timestamp = datetime.now().isoformat()
            trade.status = "closed"
            trade.pnl = pnl
            trade.pnl_pct = pnl_pct
            trade.exit_reason = reason
            trade.quantity = close_quantity
            trade.actual_quantity = close_quantity
            trade.r_multiple = r_multiple
            trade.side = side

            fees = 0.0
            for oid in filter(
                None, [getattr(trade, "alpaca_order_id", None), order.order_id]
            ):
                details = self.broker.get_order_details(oid) or {}
                try:
                    fees += float(details.get("commission") or 0)
                except (TypeError, ValueError):
                    pass
            trade.fees = float(getattr(trade, "fees", 0.0) or 0.0) + fees

            self.memory.update_trade(trade, position["trade_id"])

            self.total_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1

            self.memory.update_bot_state(
                total_pnl=self.total_pnl,
                total_trades=self.total_trades,
                winning_trades=self.winning_trades,
            )
            del self.active_positions[symbol]
            return True

        logger.error(f"Failed to place {action.lower()} order for {symbol}")
        return False

    def check_stop_losses(self, bars_by_symbol: dict | None = None):
        for symbol, position in list(self.active_positions.items()):
            try:
                current_price = self.broker.get_current_price(symbol)
                if current_price <= 0:
                    continue

                side = self._position_side(position)
                stop = position["stop_loss"]
                stopped = (
                    current_price <= stop if side == "long" else current_price >= stop
                )
                if stopped:
                    self.execute_close(symbol, "Stop loss")
                    continue

                risk_unit = position.get("risk_per_unit") or 0.0
                if risk_unit <= 0:
                    risk_unit = abs(position["entry_price"] - position["stop_loss"])
                    position["risk_per_unit"] = risk_unit
                new_stop = tighten_stop(
                    side=side,
                    entry_price=position["entry_price"],
                    stop_loss=stop,
                    price=current_price,
                    risk_per_unit=risk_unit,
                    breakeven_r=self.breakeven_r,
                    trail_arm_r=self.trail_arm_r,
                    trail_distance_r=self.trail_distance_r,
                )
                improved = new_stop > stop if side == "long" else new_stop < stop
                if improved:
                    position["stop_loss"] = new_stop
                    trade = position["trade"]
                    trade.stop_loss = new_stop
                    self.memory.update_trade(trade, position["trade_id"])
                    r_now = favorable_r(
                        position["entry_price"], current_price, risk_unit, side
                    )
                    self.memory.log_event(
                        trade_id=position["trade_id"],
                        event_type="trail_update",
                        symbol=symbol,
                        price=current_price,
                        stop_loss=new_stop,
                        reason=f"Stop tightened ({r_now:.1f}R)",
                        details={"r": r_now, "side": side},
                    )
            except Exception as e:
                logger.error(f"Error checking stop loss for {symbol}: {e}")

    def scan_markets(self):
        stock_market_open = self.market_hours.is_stock_market_open()
        symbols_to_scan = self._symbols_for_this_scan(stock_market_open)

        bars_by_symbol = self.broker.get_bars_batch(symbols_to_scan, "5Min", 100)
        stock_syms = [s for s in symbols_to_scan if self._get_asset_class(s) == "stock"]
        if stock_syms:
            usable = sum(
                1
                for s in stock_syms
                if bars_by_symbol.get(s) is not None
                and not bars_by_symbol[s].empty
                and len(bars_by_symbol[s]) >= self.min_bars
            )
            if usable < max(1, len(stock_syms) // 2):
                logger.warning(
                    f"Bar coverage {usable}/{len(stock_syms)} — skipping most names"
                )
        self.check_stop_losses(bars_by_symbol)

        for symbol in symbols_to_scan:
            try:
                asset_class = self._get_asset_class(symbol)
                bars = bars_by_symbol.get(symbol)
                if bars is None or bars.empty:
                    continue

                has_position = symbol in self.active_positions
                min_bars = (
                    self.min_bars if not has_position else max(20, self.min_bars // 2)
                )
                if len(bars) < min_bars:
                    continue

                current_price = float(bars.iloc[-1]["Close"])
                if current_price <= 0:
                    continue

                if not has_position:
                    should_buy, buy_reason, stop_loss, atr_value, buy_score = (
                        self.strategy.should_buy(bars, symbol, asset_class)
                    )
                    if should_buy:
                        self.execute_open(
                            symbol,
                            current_price,
                            stop_loss,
                            buy_reason,
                            atr=atr_value,
                            side="long",
                            score=buy_score,
                        )
                        continue

                    if self.allow_shorting and asset_class == "stock":
                        (
                            should_short,
                            short_reason,
                            short_stop,
                            short_atr,
                            short_score,
                        ) = self.strategy.should_short(bars, symbol, asset_class)
                        if should_short:
                            self.execute_open(
                                symbol,
                                current_price,
                                short_stop,
                                short_reason,
                                atr=short_atr,
                                side="short",
                                score=short_score,
                            )
                else:
                    position = self.active_positions[symbol]
                    side = self._position_side(position)
                    risk_unit = position.get("risk_per_unit") or abs(
                        position["entry_price"] - position["stop_loss"]
                    )
                    r_now = favorable_r(
                        position["entry_price"], current_price, risk_unit, side
                    )
                    abandon, abandon_reason = self.strategy.should_abandon(
                        bars, symbol, asset_class, side, r_now
                    )
                    if abandon:
                        self.execute_close(symbol, abandon_reason)
                        continue
                    if side == "short":
                        should_exit, exit_reason = self.strategy.should_cover(
                            bars, symbol, position["entry_price"], asset_class
                        )
                    else:
                        should_exit, exit_reason = self.strategy.should_sell(
                            bars, symbol, position["entry_price"], asset_class
                        )
                    if should_exit:
                        self.execute_close(symbol, exit_reason)
            except Exception as e:
                logger.debug(f"Error scanning {symbol}: {e}")

    def print_status(self):
        account = self.broker.get_account()
        equity = float(
            account.get("portfolio_value")
            or account.get("equity")
            or self.initial_capital
        )
        live = {
            AlpacaBroker.normalize_symbol(pos.get("symbol", "")): pos
            for pos in (self.broker.get_positions(force=True) or [])
        }
        rows: list[PositionView] = []
        for symbol, position in self.active_positions.items():
            quote = live.get(AlpacaBroker.normalize_symbol(symbol), {})
            side = self._position_side(position)
            last = float(quote.get("current_price") or 0)
            if last <= 0:
                last = self.broker.get_current_price(symbol)
            qty = abs(float(quote.get("qty") or position["quantity"]))
            if quote.get("unrealized_pl") is not None:
                pnl = float(quote["unrealized_pl"])
                pnl_pct = float(quote.get("unrealized_plpc") or 0) * 100
            else:
                pnl, pnl_pct = unrealized_pnl(position["entry_price"], last, qty, side)
            rows.append(
                PositionView(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    entry=float(position["entry_price"]),
                    last=last,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    stop=float(position["stop_loss"]),
                    asset_class=position["asset_class"],
                )
            )
        win_rate = (
            self.winning_trades / self.total_trades if self.total_trades > 0 else 0
        )
        session = (
            "regular hours"
            if self.market_hours.is_stock_market_open()
            else "equities closed"
        )
        header = (
            f"Equity ${equity:,.0f}   realized {signed_money(self.total_pnl)}   "
            f"win {win_rate:.0%} ({self.winning_trades}/{self.total_trades})   "
            f"{len(rows)} open   {session}"
        )
        logger.info(header + "\n" + format_positions(rows))

    def run(self):
        last_status_print = 0.0
        try:
            while True:
                start_time = time.time()
                if time.time() - last_status_print >= 300:
                    self.print_status()
                    last_status_print = time.time()

                self.scan_markets()

                elapsed = time.time() - start_time
                sleep_time = max(0, self.check_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("Stopped")
        except Exception as e:
            logger.error(f"Fatal: {e}")
        finally:
            self.print_status()
