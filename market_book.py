"""
MarketBook - Per-market trading logic
Async task that manages one market's state, quotes, and fills
"""
import time
import asyncio
import math
import random
from typing import Optional, Dict, Tuple
from order_state import OrderState


def make_coid(strategy_id: str, ticker: str) -> str:
    """Generate strategy-scoped client_order_id for routing."""
    ts = int(time.time() * 1000)
    nonce = random.randint(0, 9999)
    return f"{strategy_id}:{ticker}:{ts}:{nonce:04d}"


# Import fee utilities from existing code
def kalshi_fee(contracts: int, price_dollars: float) -> float:
    """Calculate Kalshi trading fee."""
    fee = 0.0175 * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(fee * 100) / 100


def fees_roundtrip(contracts: int, entry_price: float, exit_price: float) -> float:
    """Calculate total fees for entry + exit."""
    return kalshi_fee(contracts, entry_price) + kalshi_fee(contracts, exit_price)


class MarketBook:
    """
    Per-market trading book.

    Manages:
    - Inventory and risk policy (reuses your existing classes)
    - Quote calculation and order management
    - Event queue for trade data
    - Idempotent fill processing
    """

    def __init__(
        self,
        ticker: str,
        config,
        api,
        policy_cls,
        inventory_cls,
        portfolio_q: asyncio.Queue
    ):
        self.ticker = ticker
        self.config = config
        self.api = api

        # Reuse your existing policy and inventory classes
        self.policy = policy_cls(config)
        self.inventory = inventory_cls()

        # Order state management
        self.orders = OrderState()

        # Event queue for incoming trades (bounded, drops on overflow)
        self.event_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

        # Portfolio-level events (risk triggers, fills)
        self.portfolio_q = portfolio_q

        # Price tracking
        self.mid_ema: Optional[float] = None

        # Control flags
        # Check environment variable for live trading toggle
        import os
        live_trading = os.getenv('LIVE_TRADING_ENABLED', 'true').lower() == 'true'
        self.quoting_enabled = live_trading
        self.running = True

        if not live_trading:
            print(f"[{self.ticker}] 📄 PAPER MODE - No real orders will be placed")

        # Quote cooldown tracking (per side)
        self._last_quote_ts = {'bid': 0.0, 'ask': 0.0}

        # Logging
        self.fill_log = []
        self.realization_log = []

    async def run(self):
        """Main event loop - processes incoming market data."""
        print(f"[{self.ticker}] MarketBook started")

        while self.running:
            try:
                # Wait for event with timeout
                msg = await asyncio.wait_for(self.event_q.get(), timeout=1.0)

                if msg is None:
                    # Shutdown signal
                    break

                await self._on_market_msg(msg)

            except asyncio.TimeoutError:
                # No events, continue
                continue
            except Exception as e:
                print(f"[{self.ticker}] Error in event loop: {e}")
                await asyncio.sleep(0.1)

        print(f"[{self.ticker}] MarketBook stopped")

    async def _on_market_msg(self, msg: Dict):
        """Process incoming market message (trade/ticker update)."""
        ts = msg.get('timestamp', int(time.time()))
        price_c = msg.get('price')

        if price_c is None:
            return

        px = price_c / 100

        # Update mid price EMA (your existing logic)
        alpha = self.config.MID_PRICE_EMA_ALPHA
        if self.mid_ema is None:
            self.mid_ema = px
        else:
            self.mid_ema = alpha * px + (1 - alpha) * self.mid_ema

        # Check risk policy (your existing logic)
        should_flatten, reason = self.policy.should_flatten(
            self.inventory,
            self.mid_ema,
            ts
        )

        if should_flatten:
            await self._flatten(self.mid_ema, ts, reason)
            await self._cancel_both()
            return

        # Calculate quotes based on inventory
        quote_bid, quote_ask = self._compute_quotes()

        # Update orders on exchange
        await self._update_quotes(quote_bid, quote_ask, self.config.SIZE_PER_FILL)

    def _compute_quotes(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Compute desired bid/ask quotes based on inventory state.
        Uses your existing spread and skew logic.
        """
        base = self.config.BASE_SPREAD
        mid = self.mid_ema

        if mid is None:
            return (None, None)

        net = self.inventory.net_contracts

        # Neutral inventory: quote both sides
        if abs(net) < 20:
            return (mid - base, mid + base)

        # Long inventory: prefer to sell
        if net > 20:
            skew = self.policy.get_inventory_skew(net, size_cap=100)
            return (None, mid + base * (1 + abs(skew)))

        # Short inventory: prefer to buy
        if net < -20:
            skew = self.policy.get_inventory_skew(net, size_cap=100)
            return (mid - base * (1 + abs(skew)), None)

        return (None, None)

    def _cooldown_ok(self, side: str, min_ms: int = 300) -> bool:
        """
        Check if enough time has passed since last quote update.

        Args:
            side: 'bid' or 'ask'
            min_ms: Minimum milliseconds between updates

        Returns:
            True if cooldown has passed
        """
        now = time.time()
        if (now - self._last_quote_ts[side]) * 1000 >= min_ms:
            self._last_quote_ts[side] = now
            return True
        return False

    async def _update_quotes(
        self,
        bid: Optional[float],
        ask: Optional[float],
        qty: int
    ):
        """
        Update active quotes on exchange.
        Only cancels/replaces if price changed significantly or cooldown passed.
        """
        if not self.quoting_enabled:
            return

        # Handle bid side
        if bid is None:
            # Cancel bid if we have one
            if self.orders.active_bid:
                await self.api.cancel_order(self.orders.active_bid['order_id'])
                self.orders.clear_active_bid()
        else:
            # Place or replace bid if needed
            if self._should_replace('bid', bid) and self._cooldown_ok('bid'):
                await self._place_or_replace('bid', bid, qty)

        # Handle ask side
        if ask is None:
            # Cancel ask if we have one
            if self.orders.active_ask:
                await self.api.cancel_order(self.orders.active_ask['order_id'])
                self.orders.clear_active_ask()
        else:
            # Place or replace ask if needed
            if self._should_replace('ask', ask) and self._cooldown_ok('ask'):
                await self._place_or_replace('ask', ask, qty)

    def _should_replace(self, side: str, new_price: float, min_delta_cents: int = 1) -> bool:
        """
        Check if order should be replaced based on price change.

        Args:
            side: 'bid' or 'ask'
            new_price: New desired price in dollars
            min_delta_cents: Minimum price change to trigger replacement

        Returns:
            True if order should be replaced
        """
        active = self.orders.active_bid if side == 'bid' else self.orders.active_ask

        if not active:
            return True  # No active order, should place

        old_price = active['price']
        delta_cents = abs(new_price - old_price) * 100

        return delta_cents >= min_delta_cents

    async def _place_or_replace(self, side: str, price: float, qty: int):
        """
        Place or replace order on one side.

        Args:
            side: 'bid' or 'ask'
            price: Price in dollars
            qty: Quantity in contracts
        """
        # Cancel existing order if present
        active = self.orders.active_bid if side == 'bid' else self.orders.active_ask
        if active:
            await self.api.cancel_order(active['order_id'])

        # Place new order with strategy-tagged client_order_id
        cents = int(round(price * 100))
        coid = make_coid("MMv2", self.ticker)

        if side == 'bid':
            od = await self.api.place_order(
                self.ticker,
                side="yes",
                action="buy",
                count=qty,
                price_cents=cents,
                client_order_id=coid
            )
            if od and 'order' in od:
                order_id = od['order']['order_id']
                self.orders.active_bid = {
                    'order_id': order_id,
                    'client_order_id': coid,
                    'price': price,
                    'qty': qty,
                    'filled_count': 0
                }
                # Register for routing
                self.orders.register_order(coid, {
                    "order_id": order_id,
                    "strategy_id": "MMv2",
                    "ticker": self.ticker,
                    "side": "bid"
                })
                print(f"[{self.ticker}] BID placed: {qty} @ ${price:.4f} ({cents}¢)")
        else:  # ask
            od = await self.api.place_order(
                self.ticker,
                side="yes",
                action="sell",
                count=qty,
                price_cents=cents,
                client_order_id=coid
            )
            if od and 'order' in od:
                order_id = od['order']['order_id']
                self.orders.active_ask = {
                    'order_id': order_id,
                    'client_order_id': coid,
                    'price': price,
                    'qty': qty,
                    'filled_count': 0
                }
                # Register for routing
                self.orders.register_order(coid, {
                    "order_id": order_id,
                    "strategy_id": "MMv2",
                    "ticker": self.ticker,
                    "side": "ask"
                })
                print(f"[{self.ticker}] ASK placed: {qty} @ ${price:.4f} ({cents}¢)")

    async def _flatten(self, exit_px: float, ts: int, reason: str):
        """
        Flatten entire inventory (risk policy trigger).

        Args:
            exit_px: Exit price
            ts: Timestamp
            reason: Trigger reason
        """
        realizations = self.inventory.flatten_all(exit_px, ts, fees=0.0)
        total_pnl = sum(r['pnl'] for r in realizations)

        print(f"[{self.ticker}] FLATTEN: {reason} | P&L: ${total_pnl:+.2f}")

        # Send to portfolio queue
        await self.portfolio_q.put({
            'type': 'trigger',
            'ticker': self.ticker,
            'reason': reason,
            'pnl': total_pnl
        })

    async def _cancel_both(self):
        """Cancel all active orders."""
        if self.orders.active_bid:
            await self.api.cancel_order(self.orders.active_bid['order_id'])
            self.orders.clear_active_bid()

        if self.orders.active_ask:
            await self.api.cancel_order(self.orders.active_ask['order_id'])
            self.orders.clear_active_ask()

    async def process_fill(self, fill: Dict):
        """
        Process a fill from the exchange (idempotent).

        Args:
            fill: Fill dict from /portfolio/fills API
        """
        oid = fill.get('order_id')
        fid = fill.get('fill_id')
        ticker = fill.get('ticker')
        coid = fill.get('client_order_id', '')

        # Validate: must belong to this strategy
        if coid and not coid.startswith('MMv2:'):
            return  # Foreign strategy fill

        # Validate ticker
        if ticker != self.ticker or not oid or not fid:
            return

        # Check if already processed (idempotent)
        if self.orders.already_processed(oid, fid):
            return

        # Extract fill details
        side = fill.get('side')  # 'yes' or 'no'
        action = fill.get('action')  # 'buy' or 'sell'
        count = fill.get('count', 0)
        price_cents = fill.get('yes_price') if side == 'yes' else fill.get('no_price')

        if not all([side, action, count, price_cents]):
            return

        price = price_cents / 100
        timestamp = fill.get('created_time', int(time.time()))

        # Process based on side and action
        if action == 'buy' and side == 'yes':
            # Opening long position
            self._execute_fill(count, price, timestamp, 'long')
        elif action == 'sell' and side == 'yes':
            # Closing long position
            if self.inventory.net_contracts > 0:
                fees = fees_roundtrip(count, self.inventory.vwap_entry, price)
                self._realize_position(count, price, timestamp, fees, 'fill_from_exchange')
        elif action == 'buy' and side == 'no':
            # Opening short position
            self._execute_fill(count, price, timestamp, 'short')
        elif action == 'sell' and side == 'no':
            # Closing short position
            if self.inventory.net_contracts < 0:
                fees = fees_roundtrip(count, abs(self.inventory.vwap_entry), price)
                self._realize_position(count, price, timestamp, fees, 'fill_from_exchange')

        # Send fill event to portfolio
        await self.portfolio_q.put({
            'type': 'fill',
            'ticker': self.ticker,
            'side': side,
            'action': action,
            'qty': count,
            'price': price
        })

    def _execute_fill(self, qty: int, price: float, timestamp: int, side: str):
        """Execute a new fill (open position)."""
        entry_fee = kalshi_fee(qty, price)
        self.inventory.add_fill(qty, price, timestamp, side)

        self.fill_log.append({
            'timestamp': timestamp,
            'qty': qty,
            'price': price,
            'side': side,
            'fee': entry_fee,
            'net_contracts_after': self.inventory.net_contracts,
            'vwap_after': self.inventory.vwap_entry
        })

        print(f"[{self.ticker}] FILL: {side.upper()} {qty} @ ${price:.4f} | Net: {self.inventory.net_contracts:+d}")

    def _realize_position(self, qty: int, exit_price: float, timestamp: int, fees: float, reason: str):
        """Realize P&L on position."""
        realizations = self.inventory.realize_pnl(qty, exit_price, timestamp, fees)

        for r in realizations:
            r['exit_reason'] = reason
            r['remaining_net_contracts'] = self.inventory.net_contracts
            self.realization_log.append(r)

        total_pnl = sum(r['pnl'] for r in realizations)
        print(f"[{self.ticker}] REALIZE: Closed {qty} @ ${exit_price:.4f} | P&L: ${total_pnl:+.2f}")

    async def shutdown(self):
        """Graceful shutdown - cancel orders and stop quoting."""
        self.quoting_enabled = False
        self.running = False
        await self._cancel_both()
        await self.event_q.put(None)  # Shutdown signal

    def get_status(self) -> Dict:
        """Get current market book status."""
        return {
            'ticker': self.ticker,
            'net_contracts': self.inventory.net_contracts,
            'vwap_entry': self.inventory.vwap_entry,
            'realized_pnl': self.inventory.realized_pnl,
            'mid_ema': self.mid_ema,
            'active_bid': self.orders.active_bid,
            'active_ask': self.orders.active_ask,
            'quoting_enabled': self.quoting_enabled
        }
