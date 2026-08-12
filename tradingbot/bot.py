"""Main trading loop: scan, size, execute, trail stops, restore state."""

import logging
import re
import time
from datetime import datetime
from typing import Any

from tradingbot.broker import AlpacaBroker
from tradingbot.config import load_config
from tradingbot.market_hours import MarketHours
from tradingbot.memory import TradeMemory
from tradingbot.models import Order, OrderType, Trade
from tradingbot.strategy import SignalStrategy
from tradingbot.universe import PRIORITY_SYMBOLS

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(
        self,
        broker: AlpacaBroker,
        config: dict[str, Any] | None = None,
        data_dir: str | None = None,
    ):
        self.config = config or load_config()
        self.broker = broker
        self.initial_capital = float(self.config.get("initial_capital", 1000.0))
        self.market_hours = MarketHours(broker)
        self.memory = TradeMemory(
            data_dir or self.config.get("data_dir", "trading_data")
        )

        risk = self.config.get("risk", {})
        symbols_cfg = self.config.get("symbols", {})
        strategy_cfg = self.config.get("strategy", {})

        self.daily_crypto_limit = float(risk.get("daily_crypto_limit", 2500.0))
        self.daily_stock_limit = float(risk.get("daily_stock_limit", 5000.0))
        self.scan_batch_size = int(symbols_cfg.get("scan_batch_size", 40))
        self._scan_offset = 0

        self.stock_symbols = self._resolve_stock_symbols(symbols_cfg.get("stocks"))
        self.crypto_symbols = [
            str(s) for s in symbols_cfg.get("crypto", ["BTC/USD", "ETH/USD"])
        ]

        self.strategy = SignalStrategy(self.memory, strategy_cfg)

        self.check_interval = int(risk.get("check_interval_seconds", 45))
        self.max_positions = int(risk.get("max_positions", 6))
        self.min_hold_time = int(risk.get("min_hold_seconds", 180))
        self.crypto_risk_per_trade = float(risk.get("crypto_per_trade", 0.012))
        self.stock_risk_per_trade = float(risk.get("stock_per_trade", 0.012))
        self.max_position_value_crypto = float(
            risk.get("max_position_value_crypto", 200.0)
        )
        self.max_position_value_stock = float(
            risk.get("max_position_value_stock", 300.0)
        )
        self.min_position_value = float(risk.get("min_position_value", 15.0))
        self.allow_shorting = bool(risk.get("allow_shorting", True))

        self.atr_trail_mult = float(strategy_cfg.get("atr_trail_mult", 2.0))
        self.trail_arm_atr_mult = float(strategy_cfg.get("trail_arm_atr_mult", 1.5))

        self.active_positions: dict[str, dict[str, Any]] = {}
        self._restore_state()

        bot_state = self.memory.get_bot_state()
        self.total_pnl = bot_state.get("total_pnl", 0.0) or 0.0
        self.total_trades = bot_state.get("total_trades", 0) or 0
        self.winning_trades = bot_state.get("winning_trades", 0) or 0

        logger.info("TRADING BOT INITIALIZED")
        scan_mode = (
            "full scan every loop"
            if len(self.stock_symbols) <= self.scan_batch_size
            else f"rotate {self.scan_batch_size}/loop"
        )
        logger.info(
            f"Watchlist: {len(self.stock_symbols)} stocks + "
            f"{len(self.crypto_symbols)} crypto | {scan_mode}"
        )
        logger.info(
            f"Daily limits: Crypto ${self.daily_crypto_limit:,.0f}, "
            f"Stocks ${self.daily_stock_limit:,.0f} | max positions {self.max_positions} | "
            f"shorting={'ON' if self.allow_shorting else 'OFF'} (stocks only)"
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

    def _restore_state(self):
        memory_positions = self.memory.get_open_trades()
        alpaca_positions = self.broker.get_positions(force=True)
        alpaca_symbols = {
            pos["symbol"]: pos for pos in alpaca_positions if float(pos["qty"]) != 0
        }

        self.active_positions = {}
        ghost_positions = []
        restored_positions = []

        for symbol, (trade, trade_id) in memory_positions.items():
            if symbol in alpaca_symbols:
                alpaca_pos = alpaca_symbols[symbol]
                signed_qty = float(alpaca_pos["qty"])
                qty_abs = abs(signed_qty)
                side = getattr(trade, "side", None) or (
                    "short" if signed_qty < 0 else "long"
                )
                trade.side = side

                if self._get_asset_class(symbol) == "crypto":
                    if abs(qty_abs - abs(trade.quantity or 0)) > 0.0001:
                        logger.info(
                            f"Updating {symbol} quantity: {trade.quantity:.6f} -> {qty_abs:.6f}"
                        )
                        trade.quantity = qty_abs
                        trade.actual_quantity = qty_abs
                        self.memory.update_trade(trade, trade_id)
                elif abs(qty_abs - abs(trade.quantity or 0)) > 0.0001:
                    trade.quantity = qty_abs
                    trade.actual_quantity = qty_abs
                    self.memory.update_trade(trade, trade_id)

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
                    "side": side,
                }
                restored_positions.append(f"{symbol}({side})")
            else:
                ghost_positions.append((symbol, trade, trade_id))

        for symbol, trade, trade_id in ghost_positions:
            trade.exit_price = trade.entry_price
            trade.exit_timestamp = datetime.now().isoformat()
            trade.status = "closed"
            trade.pnl = 0.0
            trade.pnl_pct = 0.0
            trade.exit_reason = "Position not found - auto-closed"
            self.memory.update_trade(trade, trade_id)

        logger.info(
            f"Restored {len(restored_positions)} positions, cleaned {len(ghost_positions)} ghosts"
        )
        if restored_positions:
            logger.info(f"Active positions: {', '.join(restored_positions)}")

    def get_daily_spending(self, asset_class: str) -> float:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_spending = 0.0
        for trade in self.memory.get_trade_history(days=1):
            if not trade.timestamp:
                continue
            trade_date = trade.timestamp[:10]
            if (
                trade_date == today
                and trade.asset_class == asset_class
                and trade.status in ("open", "closed")
            ):
                daily_spending += trade.entry_price * abs(trade.quantity)
        return daily_spending

    def calculate_position_size(
        self, symbol: str, entry_price: float, stop_loss: float, asset_class: str
    ) -> float:
        account = self.broker.get_account()
        buying_power = float(account.get("buying_power", self.initial_capital))

        risk_per_trade = (
            self.crypto_risk_per_trade
            if asset_class == "crypto"
            else self.stock_risk_per_trade
        )
        risk_amount = buying_power * risk_per_trade

        stop_distance = abs(entry_price - stop_loss)
        if stop_distance <= 0:
            return 0

        position_size = risk_amount / stop_distance

        daily_spending = self.get_daily_spending(asset_class)
        daily_limit = (
            self.daily_crypto_limit
            if asset_class == "crypto"
            else self.daily_stock_limit
        )
        remaining_budget = daily_limit - daily_spending
        if remaining_budget < 20:
            return 0

        position_value = position_size * entry_price
        if position_value > remaining_budget:
            position_size = remaining_budget / entry_price

        if asset_class == "crypto":
            min_shares = self.min_position_value / entry_price
            position_size = max(position_size, min_shares)
            position_size = min(
                position_size, self.max_position_value_crypto / entry_price
            )
        else:
            position_size = max(1, int(position_size))
            position_size = min(
                position_size, int(self.max_position_value_stock / entry_price) or 1
            )

        return position_size

    def execute_open(
        self,
        symbol: str,
        current_price: float,
        stop_loss: float,
        reason: str,
        atr: float = 0.0,
        side: str = "long",
    ):
        """Open a long (BUY) or short (SELL) stock/crypto position."""
        side = "short" if side == "short" else "long"
        if len(self.active_positions) >= self.max_positions:
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

        daily_spending = self.get_daily_spending(asset_class)
        daily_limit = (
            self.daily_crypto_limit
            if asset_class == "crypto"
            else self.daily_stock_limit
        )
        if daily_spending >= daily_limit:
            return False

        position_size = self.calculate_position_size(
            symbol, current_price, stop_loss, asset_class
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
            f"{action} {symbol}: {position_size:.4f} @ ${current_price:.2f} | "
            f"stop ${stop_loss:.2f} (ATR {atr:.4f}) | risk ${initial_risk:.2f} | {reason}"
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
            logger.warning(
                f"No matching {side} {symbol} position in Alpaca, cleaning up memory"
            )
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
        # Closing a long = sell; covering a short = buy
        order_type = OrderType.SELL if side == "long" else OrderType.BUY
        if (
            asset_class == "crypto"
            and side == "long"
            and close_quantity * current_price < 1.0
        ):
            order = Order(
                symbol=symbol,
                order_type=order_type,
                quantity=close_quantity,
                notional=close_quantity * current_price,
                asset_class=asset_class,
            )
        else:
            order = Order(
                symbol=symbol,
                order_type=order_type,
                quantity=close_quantity,
                asset_class=asset_class,
            )

        if side == "long":
            pnl = (current_price - position["entry_price"]) * close_quantity
            pnl_pct = (
                (current_price - position["entry_price"])
                / position["entry_price"]
                * 100
            )
        else:
            pnl = (position["entry_price"] - current_price) * close_quantity
            pnl_pct = (
                (position["entry_price"] - current_price)
                / position["entry_price"]
                * 100
            )
        initial_risk = position.get("initial_risk") or 0.0
        r_multiple = (pnl / initial_risk) if initial_risk > 0 else None

        action = "SELL" if side == "long" else "COVER"
        logger.info(
            f"{action} {symbol}: {close_quantity:.6f} @ ${current_price:.2f} | "
            f"P&L: ${pnl:.2f} ({pnl_pct:.1f}%) R={r_multiple if r_multiple is not None else 'n/a'} | {reason}"
        )

        if self.broker.place_order(order):
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
                    logger.warning(
                        f"STOP LOSS ({side}): {symbol} @ ${current_price:.2f} "
                        f"(stop ${stop:.2f})"
                    )
                    self.execute_close(symbol, "Stop loss")
                    continue

                atr_value = position.get("atr") or 0.0
                if atr_value <= 0 and bars_by_symbol and symbol in bars_by_symbol:
                    snapshot = self.strategy.analyze_market(
                        bars_by_symbol[symbol], symbol, position["asset_class"]
                    )
                    atr_value = snapshot.get("atr", 0.0) or 0.0
                    position["atr"] = atr_value

                if atr_value <= 0:
                    continue

                if side == "long":
                    profit = current_price - position["entry_price"]
                    if profit >= self.trail_arm_atr_mult * atr_value:
                        new_stop = current_price - (self.atr_trail_mult * atr_value)
                        if new_stop > position["stop_loss"]:
                            position["stop_loss"] = new_stop
                            trade = position["trade"]
                            trade.stop_loss = new_stop
                            self.memory.update_trade(trade, position["trade_id"])
                            self.memory.log_event(
                                trade_id=position["trade_id"],
                                event_type="trail_update",
                                symbol=symbol,
                                price=current_price,
                                stop_loss=new_stop,
                                reason=f"ATR trail armed (+{profit / atr_value:.1f} ATR)",
                                details={"atr": atr_value, "side": side},
                            )
                else:
                    profit = position["entry_price"] - current_price
                    if profit >= self.trail_arm_atr_mult * atr_value:
                        new_stop = current_price + (self.atr_trail_mult * atr_value)
                        if new_stop < position["stop_loss"]:
                            position["stop_loss"] = new_stop
                            trade = position["trade"]
                            trade.stop_loss = new_stop
                            self.memory.update_trade(trade, position["trade_id"])
                            self.memory.log_event(
                                trade_id=position["trade_id"],
                                event_type="trail_update",
                                symbol=symbol,
                                price=current_price,
                                stop_loss=new_stop,
                                reason=f"ATR trail armed (+{profit / atr_value:.1f} ATR)",
                                details={"atr": atr_value, "side": side},
                            )
            except Exception as e:
                logger.error(f"Error checking stop loss for {symbol}: {e}")

    def scan_markets(self):
        stock_market_open = self.market_hours.is_stock_market_open()
        symbols_to_scan = self._symbols_for_this_scan(stock_market_open)

        bars_by_symbol = self.broker.get_bars_batch(symbols_to_scan, "5Min", 100)
        self.check_stop_losses(bars_by_symbol)

        for symbol in symbols_to_scan:
            try:
                asset_class = self._get_asset_class(symbol)
                bars = bars_by_symbol.get(symbol)
                if bars is None or bars.empty:
                    continue

                has_position = symbol in self.active_positions
                min_bars = 40 if not has_position else 20
                if len(bars) < min_bars:
                    continue

                current_price = float(bars.iloc[-1]["Close"])
                if current_price <= 0:
                    continue

                if not has_position:
                    should_buy, buy_reason, stop_loss, atr_value = (
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
                        )
                        continue

                    if self.allow_shorting and asset_class == "stock":
                        should_short, short_reason, short_stop, short_atr = (
                            self.strategy.should_short(bars, symbol, asset_class)
                        )
                        if should_short:
                            self.execute_open(
                                symbol,
                                current_price,
                                short_stop,
                                short_reason,
                                atr=short_atr,
                                side="short",
                            )
                else:
                    position = self.active_positions[symbol]
                    side = self._position_side(position)
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
        portfolio_value = float(account.get("portfolio_value", self.initial_capital))
        win_rate = (
            self.winning_trades / self.total_trades if self.total_trades > 0 else 0
        )

        stock_positions = {
            k: v
            for k, v in self.active_positions.items()
            if v["asset_class"] == "stock"
        }
        crypto_positions = {
            k: v
            for k, v in self.active_positions.items()
            if v["asset_class"] == "crypto"
        }

        logger.info("=" * 60)
        logger.info(f"BOT STATUS - {datetime.now().strftime('%H:%M:%S')}")
        logger.info(
            f"Portfolio: ${portfolio_value:,.2f} | P&L: ${self.total_pnl:.2f} | "
            f"Win Rate: {win_rate:.1%} ({self.winning_trades}/{self.total_trades})"
        )
        logger.info(
            f"Positions: {len(self.active_positions)}/{self.max_positions} | "
            f"Stocks: {len(stock_positions)} | Crypto: {len(crypto_positions)}"
        )

        for label, positions in (
            ("STOCKS", stock_positions),
            ("CRYPTO", crypto_positions),
        ):
            if not positions:
                continue
            logger.info(f"  {label}:")
            for symbol, position in positions.items():
                current_price = self.broker.get_current_price(symbol)
                side = self._position_side(position)
                qty = abs(
                    self.broker.get_actual_position_quantity(symbol)
                    if position["asset_class"] == "crypto"
                    else position["quantity"]
                )
                if side == "long":
                    unrealized_pnl = (current_price - position["entry_price"]) * qty
                    pnl_pct = (
                        (current_price - position["entry_price"])
                        / position["entry_price"]
                        * 100
                    )
                else:
                    unrealized_pnl = (position["entry_price"] - current_price) * qty
                    pnl_pct = (
                        (position["entry_price"] - current_price)
                        / position["entry_price"]
                        * 100
                    )
                hold_time = datetime.now() - position["timestamp"]
                qty_fmt = (
                    f"{qty:.6f}"
                    if position["asset_class"] == "crypto"
                    else f"{qty:.0f}"
                )
                logger.info(
                    f"    {symbol} [{side}]: {qty_fmt} @ ${position['entry_price']:.2f} | "
                    f"Current: ${current_price:.2f} | P&L: ${unrealized_pnl:.2f} ({pnl_pct:.1f}%) | "
                    f"Stop: ${position['stop_loss']:.2f} | Hold: {str(hold_time).split('.')[0]}"
                )

        logger.info("=" * 60)

    def run(self):
        logger.info("STARTING TRADING BOT")
        logger.info(
            f"ATR stops | long+short stocks | {self.max_positions} max positions | "
            f"{self.check_interval}s intervals | Alpaca market data"
        )

        last_status_print = time.time()
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
            logger.info("Trading stopped by user")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
        finally:
            self.print_status()
            logger.info("Bot shutdown complete")
