"""
MultiMarketMaker - Portfolio-level orchestrator
Manages multiple MarketBook instances with single async event loop
"""
import asyncio
import json
import time
import websockets
from typing import Dict, List
from kalshi_async_client import KalshiAsyncClient
from market_book import MarketBook


class MultiMarketMaker:
    """
    Portfolio-level orchestrator for multi-market trading.

    Manages:
    - Multiple MarketBook instances (one per market)
    - Single WebSocket connection with multiplexing
    - Portfolio-wide fill reconciliation
    - Cross-market risk monitoring
    """

    def __init__(
        self,
        config,
        tickers: List[str],
        policy_cls,
        inventory_cls
    ):
        self.config = config
        self.tickers = tickers

        # Create async API client (shared across all markets)
        self.api = KalshiAsyncClient(
            base_url=config.KALSHI_BASE_URL,
            ws_url=config.KALSHI_WS_URL,
            api_key=config.KALSHI_API_KEY,
            api_secret_pem=config.KALSHI_API_SECRET
        )

        # Portfolio-level event queue
        self.portfolio_q: asyncio.Queue = asyncio.Queue()

        # Create MarketBook for each ticker
        self.books: Dict[str, MarketBook] = {}
        for ticker in tickers:
            self.books[ticker] = MarketBook(
                ticker=ticker,
                config=config,
                api=self.api,
                policy_cls=policy_cls,
                inventory_cls=inventory_cls,
                portfolio_q=self.portfolio_q
            )

        # Control flags
        self.running = True
        self.shutdown_event = asyncio.Event()

        # Discord webhook for alerts
        self.discord_webhook = getattr(config, 'DISCORD_WEBHOOK_URL', None)

        print(f"📊 MultiMarketMaker initialized with {len(tickers)} markets")

    async def run(self):
        """Main entry point - starts all tasks."""
        print("🚀 Starting MultiMarketMaker...")

        try:
            # Start all tasks
            tasks = [
                self.ws_manager(),
                self.portfolio_loop(),
                self.fill_reconciliation_loop(),
            ]

            # Add MarketBook tasks with staggered activation
            for i, (ticker, book) in enumerate(self.books.items()):
                tasks.append(book.run())
                # Stagger activation: add 2 markets every 60s
                if (i + 1) % 2 == 0 and i < len(self.books) - 1:
                    print(f"⏸️  Staggered start: activated {i+1} markets, waiting 5s...")
                    await asyncio.sleep(5)  # Shorter for testing

            # Run all tasks
            await asyncio.gather(*tasks, return_exceptions=True)

        except KeyboardInterrupt:
            print("\n⚠️  Keyboard interrupt received")
            await self.shutdown()
        except Exception as e:
            print(f"❌ Fatal error: {e}")
            await self.shutdown()

    async def ws_manager(self):
        """
        WebSocket manager with automatic reconnection.
        Multiplexes messages to correct MarketBook.
        """
        reconnect_delay = 1.0
        max_delay = 60.0

        while self.running:
            try:
                print(f"🔌 Connecting to WebSocket: {self.config.KALSHI_WS_URL}")

                # Build WebSocket connection with auth headers
                headers = self._ws_headers()

                async with websockets.connect(
                    self.config.KALSHI_WS_URL,
                    additional_headers=headers,  # Changed from extra_headers
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    print("✅ WebSocket connected")

                    # Subscribe to all markets
                    await self._subscribe_markets(ws, list(self.books.keys()))

                    # Reset reconnect delay on successful connection
                    reconnect_delay = 1.0

                    # Process messages
                    async for msg in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(msg)
                            await self._route_message(data)
                        except Exception as e:
                            print(f"⚠️  Error processing WS message: {e}")

            except Exception as e:
                if self.running:
                    print(f"❌ WebSocket error: {e}")
                    print(f"🔄 Reconnecting in {reconnect_delay:.1f}s...")
                    await asyncio.sleep(reconnect_delay)

                    # Exponential backoff
                    reconnect_delay = min(reconnect_delay * 2, max_delay)
                else:
                    break

        print("🔌 WebSocket manager stopped")

    def _ws_headers(self) -> Dict[str, str]:
        """Generate signed headers for WebSocket connection."""
        path = "/trade-api/ws/v2"
        ts = str(int(time.time() * 1000))
        sig = self.api._sig(ts, "GET", path)

        return {
            "KALSHI-ACCESS-KEY": self.config.KALSHI_API_KEY,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts
        }

    async def _subscribe_markets(self, ws, tickers: List[str]):
        """Subscribe to ticker and trade channels for all markets."""
        # Global ticker channel
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["ticker"]}
        }))
        print("📡 Subscribed to global ticker channel")

        # Per-market trade channels
        for i, ticker in enumerate(tickers, start=2):
            await ws.send(json.dumps({
                "id": i,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta", "trade"],
                    "market_ticker": ticker
                }
            }))
            print(f"📡 Subscribed to {ticker}")

    async def _route_message(self, data: Dict):
        """Route WebSocket message to correct MarketBook."""
        msg_type = data.get("type")

        if msg_type in ("trade", "ticker"):
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")

            if ticker and ticker in self.books:
                # Normalize event format
                event = {
                    'timestamp': msg.get('ts', int(time.time())),
                    'price': msg.get('price'),
                    'taker_side': msg.get('taker_side'),
                    'count': msg.get('count', 10)
                }

                # Try to push to market's event queue (non-blocking)
                try:
                    self.books[ticker].event_q.put_nowait(event)
                except asyncio.QueueFull:
                    # Backpressure: drop event if queue full
                    pass

    async def portfolio_loop(self):
        """
        Portfolio-level monitoring and risk management.
        Processes events from all MarketBooks.
        """
        print("👁️  Portfolio monitor started")

        last_heartbeat = time.time()
        heartbeat_interval = 60  # Log status every 60 seconds

        while self.running:
            try:
                # Wait for events with timeout
                evt = await asyncio.wait_for(self.portfolio_q.get(), timeout=1.0)

                evt_type = evt.get('type')

                if evt_type == 'trigger':
                    # Risk policy triggered on a market
                    ticker = evt.get('ticker')
                    reason = evt.get('reason')
                    pnl = evt.get('pnl', 0)

                    msg = f"🚨 **RISK TRIGGER**\n{ticker}\nReason: {reason}\nP&L: ${pnl:+.2f}"
                    await self._send_discord_alert(msg)
                    print(f"⚠️  [{ticker}] Risk trigger: {reason}")

                elif evt_type == 'fill':
                    # Fill received
                    ticker = evt.get('ticker')
                    side = evt.get('side')
                    action = evt.get('action')
                    qty = evt.get('qty')
                    price = evt.get('price')

                    emoji = "🟢" if action == "buy" else "🔴"
                    book = self.books.get(ticker)
                    net = book.inventory.net_contracts if book else 0

                    msg = (f"{emoji} **FILL**\n{ticker}\n"
                           f"{action.upper()} {side.upper()} {qty} @ ${price:.4f}\n"
                           f"Net: {net:+d}")
                    await self._send_discord_alert(msg)

                # Portfolio-level risk checks
                await self._check_portfolio_risk()

            except asyncio.TimeoutError:
                # No events, log periodic heartbeat
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    await self._log_heartbeat()
                    last_heartbeat = now
                continue
            except Exception as e:
                print(f"⚠️  Portfolio loop error: {e}")
                await asyncio.sleep(0.1)

        print("👁️  Portfolio monitor stopped")

    async def _log_heartbeat(self):
        """Log periodic status update showing bot is alive and monitoring."""
        print("=" * 60)
        print("💓 HEARTBEAT - Bot Status")
        print("=" * 60)

        for ticker, book in self.books.items():
            # Get current state
            net = book.inventory.net_contracts
            vwap = book.inventory.vwap_entry if net != 0 else None
            mid = book.mid_ema
            realized_pnl = book.inventory.realized_pnl

            # Order status
            bid_status = f"{book.orders.active_bid['qty']}@{book.orders.active_bid['price']:.4f}" if book.orders.active_bid else "None"
            ask_status = f"{book.orders.active_ask['qty']}@{book.orders.active_ask['price']:.4f}" if book.orders.active_ask else "None"

            print(f"\n[{ticker}]")
            print(f"  Mid: ${mid:.4f}" if mid else "  Mid: N/A")
            print(f"  Position: {net:+d} contracts" + (f" (VWAP: ${vwap:.4f})" if vwap else ""))
            print(f"  Realized P&L: ${realized_pnl:+.2f}")
            print(f"  Active Orders: BID={bid_status} | ASK={ask_status}")
            print(f"  Quoting: {'✅ Active' if book.quoting_enabled else '⏸️  Paused'}")

        print("=" * 60)

    async def _check_portfolio_risk(self):
        """Check for portfolio-level risk conditions."""
        # Count markets in MAE breach
        mae_breaches = sum(
            1 for book in self.books.values()
            if book.policy.mae_breach_start is not None
        )

        # If 3+ markets in MAE breach simultaneously, reduce sizes
        if mae_breaches >= 3:
            msg = f"🚨 **PORTFOLIO ALERT**\n{mae_breaches} markets in MAE breach"
            await self._send_discord_alert(msg)
            print(f"⚠️  Portfolio risk: {mae_breaches} markets in MAE breach")

    async def fill_reconciliation_loop(self):
        """
        Monitor order status for all active orders.
        Uses order status API instead of fills API (more reliable).
        """
        print("🔄 Order monitoring started")

        while self.running:
            try:
                # Check order status for all markets with pending orders
                for book in self.books.values():
                    await self._check_order_status(book)

            except Exception as e:
                print(f"⚠️  Order monitoring error: {e}")
                await asyncio.sleep(1.0)

            # Poll every 2 seconds
            await asyncio.sleep(2.0)

        print("🔄 Order monitoring stopped")

    async def _check_order_status(self, book):
        """Check status of active orders for a market book and process fills."""
        # Check bid order
        if book.orders.active_bid:
            status = await self.api.get_order_status(book.orders.active_bid['order_id'])

            if status is None:
                # 404 - order filled or cancelled
                print(f"[{book.ticker}] BID order filled/cancelled (404)")
                book.orders.clear_active_bid()
            elif status:
                order = status.get('order', {})
                order_id = order.get('order_id')
                filled_count = order.get('filled_count', 0)
                total_count = order.get('count', 0)
                price = order.get('yes_price')
                order_status = order.get('status', '')

                # Detect NEW fills (partial or full)
                prev_filled = book.orders.active_bid.get('filled_count', 0)
                new_fills = filled_count - prev_filled

                if new_fills > 0:
                    # New fill detected! Create fill object and process it
                    print(f"[{book.ticker}] 🟢 BID FILL: {new_fills} contracts @ {price}¢ (total: {filled_count}/{total_count})")

                    fill = {
                        'order_id': order_id,
                        'fill_id': f"{order_id}_{filled_count}",  # Synthetic fill_id
                        'ticker': book.ticker,
                        'side': 'yes',
                        'action': 'buy',
                        'count': new_fills,
                        'yes_price': price,
                        'no_price': None,
                        'created_time': int(time.time())
                    }

                    # Process the fill (creates position, places exit order)
                    await book.process_fill(fill)

                    # Update tracked filled_count
                    book.orders.active_bid['filled_count'] = filled_count

                # Clear order if fully filled or cancelled
                if order_status in ['filled', 'executed', 'cancelled'] or filled_count == total_count:
                    if filled_count == total_count:
                        print(f"[{book.ticker}] BID order fully filled ({filled_count}/{total_count})")
                    book.orders.clear_active_bid()

        # Check ask order
        if book.orders.active_ask:
            status = await self.api.get_order_status(book.orders.active_ask['order_id'])

            if status is None:
                # 404 - order filled or cancelled
                print(f"[{book.ticker}] ASK order filled/cancelled (404)")
                book.orders.clear_active_ask()
            elif status:
                order = status.get('order', {})
                order_id = order.get('order_id')
                filled_count = order.get('filled_count', 0)
                total_count = order.get('count', 0)
                price = order.get('yes_price')
                order_status = order.get('status', '')

                # Detect NEW fills (partial or full)
                prev_filled = book.orders.active_ask.get('filled_count', 0)
                new_fills = filled_count - prev_filled

                if new_fills > 0:
                    # New fill detected! Create fill object and process it
                    print(f"[{book.ticker}] 🔴 ASK FILL: {new_fills} contracts @ {price}¢ (total: {filled_count}/{total_count})")

                    fill = {
                        'order_id': order_id,
                        'fill_id': f"{order_id}_{filled_count}",  # Synthetic fill_id
                        'ticker': book.ticker,
                        'side': 'yes',
                        'action': 'sell',
                        'count': new_fills,
                        'yes_price': price,
                        'no_price': None,
                        'created_time': int(time.time())
                    }

                    # Process the fill (creates position, places exit order)
                    await book.process_fill(fill)

                    # Update tracked filled_count
                    book.orders.active_ask['filled_count'] = filled_count

                # Clear order if fully filled or cancelled
                if order_status in ['filled', 'executed', 'cancelled'] or filled_count == total_count:
                    if filled_count == total_count:
                        print(f"[{book.ticker}] ASK order fully filled ({filled_count}/{total_count})")
                    book.orders.clear_active_ask()

    async def _send_discord_alert(self, message: str):
        """Send alert to Discord webhook."""
        if not self.discord_webhook:
            return

        try:
            await self.api.http.post(
                self.discord_webhook,
                json={"content": message}
            )
        except Exception as e:
            print(f"⚠️  Discord alert failed: {e}")

    async def shutdown(self):
        """Graceful shutdown - stop all markets and close connections."""
        print("\n🛑 Initiating graceful shutdown...")

        self.running = False

        # Stop all MarketBooks
        print("📕 Stopping all market books...")
        shutdown_tasks = [book.shutdown() for book in self.books.values()]
        await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        # Close HTTP client
        print("🔌 Closing HTTP client...")
        await self.api.close()

        print("✅ Shutdown complete")

    def get_portfolio_status(self) -> Dict:
        """Get aggregated portfolio status."""
        total_net = sum(book.inventory.net_contracts for book in self.books.values())
        total_pnl = sum(book.inventory.realized_pnl for book in self.books.values())

        market_statuses = {
            ticker: book.get_status()
            for ticker, book in self.books.items()
        }

        return {
            'total_markets': len(self.books),
            'total_net_contracts': total_net,
            'total_realized_pnl': total_pnl,
            'markets': market_statuses
        }
