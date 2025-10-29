"""
Live Market Making Bot - NHL Edition
Hybrid Policy: Duration-Weighted + MAE Failsafe

Strategy:
- Primary: Duration-weighted exposure management (time × size risk)
- Failsafe: MAE 8¢ circuit breaker (must persist >8s to avoid noise)
- Optimized for: Goal-event volatility capture with tail risk protection
"""
import sys
import time
import json
from pathlib import Path
from collections import deque
from datetime import datetime
import statistics
import math
from typing import Optional, Dict, List, Tuple
import asyncio
import websockets
import httpx
import threading
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings

# ============================================================================
# CONFIGURATION
# ============================================================================

class BotConfig:
    """Bot configuration - edit these values"""

    # Kalshi API connection
    KALSHI_API_KEY = settings.kalshi_api_key
    KALSHI_API_SECRET = settings.kalshi_api_secret
    KALSHI_BASE_URL = settings.kalshi_base_url

    # Kalshi WebSocket URL (derived from base URL)
    KALSHI_WS_URL = settings.kalshi_base_url.replace("https://", "wss://").replace("/trade-api/v2", "/trade-api/ws/v2")

    # Trading parameters
    BASE_SPREAD = settings.base_spread
    SIZE_PER_FILL = settings.size_per_fill
    MAX_INVENTORY_VALUE = settings.max_inventory_value
    QUEUE_SHARE = settings.queue_share

    # Risk policy parameters
    DURATION_WEIGHTED_LIMIT = settings.duration_weighted_limit
    MAE_FAILSAFE_CENTS = settings.mae_failsafe_cents
    MAE_ACTIVATION_WINDOW_SEC = settings.mae_activation_window_sec

    # Market selection
    SERIES_TICKER = settings.series_ticker
    MIN_SPREAD_THRESHOLD = settings.min_spread_threshold

    # EMA smoothing
    MID_PRICE_EMA_ALPHA = 0.3

    # Logging
    LOG_FILLS = True
    LOG_REALIZATIONS = True
    LOG_TRIGGERS = True

    # Database tables
    TRADES_TABLE = 'trades'
    MARKET_METADATA_TABLE = 'market_metadata'

    # Discord webhook for alerts
    DISCORD_WEBHOOK_URL = settings.discord_webhook_url

    # Pyramiding controls
    ALLOW_PYRAMIDING = True
    PYRAMID_MAX_CONTRACTS = 20        # Match backtest cap
    PYRAMID_SIZE_PER_FILL = 2         # Contracts per layer (backtest shows qty=2)
    PYRAMID_STEP_C = 2                # Grid step in cents between layers
    PYRAMID_REENTRY_COOLDOWN_S = 8    # Seconds between re-entry fills
    PYRAMID_USE_VWAP_GRID = True      # True=grid from VWAP, False=from last fill
    TICK_C = 1                        # Tick size in cents


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def kalshi_fee(contracts: int, price_dollars: float) -> float:
    """Calculate Kalshi trading fee."""
    fee = 0.0175 * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(fee * 100) / 100  # Round up to nearest cent


def fees_roundtrip(contracts: int, entry_price: float, exit_price: float) -> float:
    """Calculate total fees for entry + exit."""
    return kalshi_fee(contracts, entry_price) + kalshi_fee(contracts, exit_price)


def get_timestamp() -> int:
    """Get current Unix timestamp in seconds."""
    return int(time.time())


# ============================================================================
# INVENTORY MANAGEMENT
# ============================================================================

class Fill:
    """Represents a single fill (layer in inventory)."""

    def __init__(self, qty: int, price: float, timestamp: int, side: str):
        self.qty = qty  # Contracts
        self.price = price  # Entry price (dollars)
        self.timestamp = timestamp
        self.side = side  # 'long' or 'short'

    def age_seconds(self, current_time: int) -> int:
        """Age of this fill in seconds."""
        return current_time - self.timestamp


class InventoryState:
    """Maintains inventory with layered fills and VWAP tracking."""

    def __init__(self):
        self.layers: deque[Fill] = deque()
        self.net_contracts = 0  # Signed: positive=long, negative=short
        self.realized_pnl = 0.0
        self.total_fills = 0

        # Track realizations for metrics
        self.recent_realizations = deque(maxlen=50)

        # All realizations for logging
        self.all_realizations: List[Dict] = []

    @property
    def vwap_entry(self) -> float:
        """Volume-weighted average price of current inventory."""
        if not self.layers:
            return 0.0
        total_notional = sum(f.qty * f.price for f in self.layers)
        total_qty = sum(f.qty for f in self.layers)
        return total_notional / total_qty if total_qty > 0 else 0.0

    def inventory_mae(self, current_price: float) -> float:
        """MAE of inventory (VWAP-anchored), in dollars."""
        if not self.layers:
            return 0.0
        vwap = self.vwap_entry
        if self.net_contracts > 0:  # Long
            return max(0, vwap - current_price)
        else:  # Short
            return max(0, current_price - vwap)

    def oldest_fill_age(self, current_time: int) -> int:
        """Age of oldest fill in seconds."""
        if not self.layers:
            return 0
        return min(f.age_seconds(current_time) for f in self.layers)

    def duration_weighted_exposure(self, current_time: int) -> float:
        """Sum of |qty| × age_minutes for all layers."""
        return sum(abs(f.qty) * (f.age_seconds(current_time) / 60) for f in self.layers)

    def add_fill(self, qty: int, price: float, timestamp: int, side: str):
        """Add new fill to inventory."""
        self.layers.append(Fill(qty, price, timestamp, side))
        if side == 'long':
            self.net_contracts += qty
        else:
            self.net_contracts -= qty
        self.total_fills += 1

    def realize_pnl(self, qty: int, exit_price: float, exit_time: int, fees: float = 0.0) -> List[Dict]:
        """Realize P&L on offsetting fills (FIFO)."""
        qty_to_realize = qty
        hold_times = []
        realizations = []

        while qty_to_realize > 0 and self.layers:
            layer = self.layers[0]
            realized_qty = min(layer.qty, qty_to_realize)

            # Calculate P&L
            if layer.side == 'long':
                pnl = realized_qty * (exit_price - layer.price) - fees
            else:
                pnl = realized_qty * (layer.price - exit_price) - fees

            self.realized_pnl += pnl

            # Track hold time
            hold_time = exit_time - layer.timestamp
            hold_times.append(hold_time)

            # Record realization
            realizations.append({
                'realized_qty': realized_qty,
                'entry_price': layer.price,
                'exit_price': exit_price,
                'entry_time': layer.timestamp,
                'exit_time': exit_time,
                'hold_time_seconds': hold_time,
                'side': layer.side,
                'pnl': pnl
            })

            # Update layer or remove
            layer.qty -= realized_qty
            if layer.qty <= 0:
                self.layers.popleft()

            qty_to_realize -= realized_qty

            # Update net contracts
            if layer.side == 'long':
                self.net_contracts -= realized_qty
            else:
                self.net_contracts += realized_qty

        # Record realization metrics
        if hold_times:
            avg_hold = statistics.mean(hold_times)
            self.recent_realizations.append(avg_hold)

        # Store all realizations
        self.all_realizations.extend(realizations)

        return realizations

    def flatten_all(self, exit_price: float, exit_time: int, fees: float = 0.0) -> List[Dict]:
        """Close entire inventory at current price."""
        all_realizations = []
        while self.layers:
            layer = self.layers[0]
            realizations = self.realize_pnl(layer.qty, exit_price, exit_time, fees)
            all_realizations.extend(realizations)
        return all_realizations


# ============================================================================
# HYBRID RISK POLICY
# ============================================================================

class HybridRiskPolicy:
    """
    Hybrid inventory risk policy with persistent MAE failsafe.

    Primary: Duration-weighted exposure control
    Failsafe: MAE 8¢ limit (must persist >8 seconds)
    """

    def __init__(self, config: BotConfig):
        self.config = config

        # MAE persistence tracking
        self.mae_breach_start: Optional[int] = None
        self.mae_values_in_breach: List[float] = []

    def should_flatten(
        self,
        inventory: InventoryState,
        current_price: float,
        current_time: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if inventory should be flattened.

        Returns:
            (should_flatten, reason)
        """

        # Check duration-weighted exposure (primary)
        dwe = inventory.duration_weighted_exposure(current_time)
        if dwe >= self.config.DURATION_WEIGHTED_LIMIT:
            self._reset_mae_breach()
            return True, f'duration_weighted_{dwe:.0f}'

        # Check MAE failsafe (must persist >8 seconds)
        mae = inventory.inventory_mae(current_price)
        mae_threshold = self.config.MAE_FAILSAFE_CENTS / 100  # Convert cents to dollars

        if mae >= mae_threshold:
            # MAE breach detected
            if self.mae_breach_start is None:
                # First breach
                self.mae_breach_start = current_time
                self.mae_values_in_breach = [mae]
            else:
                # Ongoing breach
                self.mae_values_in_breach.append(mae)
                breach_duration = current_time - self.mae_breach_start

                if breach_duration >= self.config.MAE_ACTIVATION_WINDOW_SEC:
                    # Breach persisted long enough - trigger!
                    avg_mae = statistics.mean(self.mae_values_in_breach)
                    self._reset_mae_breach()
                    return True, f'mae_failsafe_{avg_mae*100:.1f}c_duration_{breach_duration}s'
        else:
            # No longer in breach - reset
            self._reset_mae_breach()

        return False, None

    def _reset_mae_breach(self):
        """Reset MAE breach tracking."""
        self.mae_breach_start = None
        self.mae_values_in_breach = []

    def get_inventory_skew(self, net_contracts: int, size_cap: int = 100) -> float:
        """Calculate quote skew based on inventory position."""
        if abs(net_contracts) < 20:
            return 0.0  # Neutral

        # Sigmoid-like skew: more extreme as we approach limits
        bias = net_contracts / size_cap
        return bias  # Positive = long (widen asks), negative = short (widen bids)


# ============================================================================
# KALSHI API CLIENT
# ============================================================================

class KalshiAPIClient:
    """Client for Kalshi REST API and WebSocket with RSA signature-based auth."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = config.KALSHI_BASE_URL
        self.ws_url = config.KALSHI_WS_URL
        self.api_key = config.KALSHI_API_KEY
        self.api_secret = config.KALSHI_API_SECRET

        self.session = httpx.Client(timeout=30.0)
        self.private_key = None
        self.ws = None
        self.ws_thread: Optional[threading.Thread] = None
        self.ws_connected = False
        self.ws_loop = None

        # Message handler
        self.on_trade_callback = None

        # Load private key
        self._load_private_key()

    def _load_private_key(self):
        """Load RSA private key from settings."""
        try:
            # Handle both actual newlines and escaped \n sequences
            key_string = self.api_secret
            if '\\n' in key_string:
                key_string = key_string.replace('\\n', '\n')

            key_bytes = key_string.encode('utf-8')
            self.private_key = serialization.load_pem_private_key(
                key_bytes,
                password=None,
                backend=default_backend()
            )
            print(f"✅ Private key loaded successfully")
        except Exception as e:
            print(f"❌ Failed to load private key: {e}")
            print(f"   Key preview: {self.api_secret[:50] if self.api_secret else 'None'}...")
            raise

    def _create_signature(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Create RSA-PSS signature for API request."""
        if not self.private_key:
            return ""

        try:
            # Message format: timestamp + method + path + body
            message = f"{timestamp}{method}{path}{body}"

            # Sign using RSA-PSS
            signature = self.private_key.sign(
                message.encode('utf-8'),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )

            return base64.b64encode(signature).decode('utf-8')

        except Exception as e:
            print(f"❌ Signature creation failed: {e}")
            return ""

    def _get_signed_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Get headers with RSA signature."""
        timestamp = str(int(time.time() * 1000))
        signature = self._create_signature(timestamp, method, path, body)

        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp
        }

    def place_order(self, market_ticker: str, side: str, action: str, count: int, price_cents: int) -> Optional[Dict]:
        """
        Place an order on Kalshi.

        Args:
            market_ticker: Market ticker symbol
            side: 'yes' or 'no'
            action: 'buy' or 'sell'
            count: Number of contracts
            price_cents: Limit price in cents

        Returns:
            Order response dict or None if failed
        """
        try:
            path = "/portfolio/orders"
            payload = {
                "ticker": market_ticker,
                "client_order_id": f"{int(time.time()*1000)}",
                "side": side,
                "action": action,
                "count": count,
                "type": "limit",
                "yes_price": price_cents if side == "yes" else None,
                "no_price": price_cents if side == "no" else None
            }

            body = json.dumps(payload)
            headers = self._get_signed_headers("POST", path, body)
            headers["Content-Type"] = "application/json"

            response = self.session.post(
                f"{self.base_url}{path}",
                content=body,
                headers=headers
            )
            response.raise_for_status()

            order_data = response.json()
            print(f"✅ Order placed: {side} {action} {count} @ {price_cents}¢")
            return order_data

        except Exception as e:
            print(f"❌ Failed to place order: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        try:
            path = f"/portfolio/orders/{order_id}"
            headers = self._get_signed_headers("DELETE", path)

            response = self.session.delete(
                f"{self.base_url}{path}",
                headers=headers
            )
            response.raise_for_status()

            print(f"✅ Order cancelled: {order_id}")
            return True

        except Exception as e:
            print(f"❌ Failed to cancel order: {e}")
            return False

    def get_fills(self, ticker: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get recent fills from portfolio."""
        try:
            path = "/portfolio/fills"
            params = {"limit": limit}
            if ticker:
                params["ticker"] = ticker

            # Build query string
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            full_path = f"{path}?{query}"

            headers = self._get_signed_headers("GET", full_path)

            response = self.session.get(
                f"{self.base_url}{full_path}",
                headers=headers
            )
            response.raise_for_status()

            data = response.json()
            return data.get("fills", [])

        except Exception as e:
            print(f"❌ Failed to get fills: {e}")
            return []

    def get_positions(self) -> List[Dict]:
        """Get current positions."""
        try:
            path = "/portfolio/positions"
            headers = self._get_signed_headers("GET", path)

            response = self.session.get(
                f"{self.base_url}{path}",
                headers=headers
            )
            response.raise_for_status()

            data = response.json()
            return data.get("positions", [])

        except Exception as e:
            print(f"❌ Failed to get positions: {e}")
            return []

    def get_active_markets(self, series_ticker: str = "KXNHLGAME", status: str = "open", limit: int = 100) -> List[Dict]:
        """
        Get active markets from Kalshi.

        Args:
            series_ticker: Series to filter (e.g., "KXNHLGAME", "KXNFLGAME", etc.)
            status: Market status ("open", "closed", etc.)
            limit: Max markets to return

        Returns:
            List of market dicts with ticker, title, volume, etc.
        """
        try:
            params = {
                "limit": limit,
                "status": status,
                "series_ticker": series_ticker
            }

            # Build query string and path for signature
            query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            path = f"/markets?{query_string}"

            headers = self._get_signed_headers("GET", path)

            print(f"🔍 Fetching markets: {self.base_url}/markets?{query_string}")

            response = self.session.get(
                f"{self.base_url}/markets",
                headers=headers,
                params=params
            )

            print(f"📡 API Response: status={response.status_code}")

            response.raise_for_status()

            data = response.json()
            markets = data.get("markets", [])

            print(f"📊 Found {len(markets)} {series_ticker} markets")

            if len(markets) == 0:
                print(f"⚠️ Response data keys: {list(data.keys())}")
                print(f"⚠️ Full response: {data}")

            return markets

        except Exception as e:
            print(f"❌ Failed to get markets: {e}")
            if hasattr(e, 'response') and e.response:
                print(f"   Status: {e.response.status_code}")
                print(f"   Response: {e.response.text[:500]}")
            return []

    def connect_websocket(self, market_ticker: str, on_trade_callback):
        """Connect to Kalshi WebSocket for real-time market data using async websockets."""
        self.on_trade_callback = on_trade_callback

        async def ws_handler():
            """Async WebSocket handler."""
            try:
                # Get signed headers for WebSocket
                ws_path = "/trade-api/ws/v2"
                headers = self._get_signed_headers("GET", ws_path)

                print(f"🔐 Connecting to WebSocket with signed headers...")
                print(f"   URL: {self.ws_url}")

                # Connect with additional_headers (newer websockets API)
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=60
                ) as websocket:
                    self.ws_connected = True
                    print(f"✅ WebSocket connected")

                    # First: Subscribe to global ticker channel (for all markets)
                    ticker_msg = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker"]  # Global ticker - no market_ticker param
                        }
                    }
                    await websocket.send(json.dumps(ticker_msg))
                    print(f"📡 Subscribed to global ticker")

                    # Second: Subscribe to market-specific trades and orderbook
                    market_msg = {
                        "id": 2,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta", "trade"],
                            "market_ticker": market_ticker
                        }
                    }
                    await websocket.send(json.dumps(market_msg))
                    print(f"📡 Subscribed to {market_ticker} (trades & orderbook)")

                    # Listen for messages
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            msg_type = data.get("type")

                            if msg_type == "trade":
                                # Process trade event (actual trade execution)
                                trade_data = data.get("msg", {})
                                if self.on_trade_callback:
                                    self.on_trade_callback(trade_data)

                            elif msg_type == "ticker":
                                # Process ticker update (price changes)
                                ticker_data = data.get("msg", {})
                                ticker_market = ticker_data.get("market_ticker", "unknown")

                                # Only process if it's our market
                                if ticker_market.lower() == market_ticker.lower():
                                    # Convert ticker to trade-like event for bot processing
                                    trade_event = {
                                        'timestamp': ticker_data.get('ts', int(time.time())),
                                        'price': ticker_data.get('price'),
                                        'taker_side': 'yes',  # Assume yes side for price updates
                                        'count': 1,  # Ticker doesn't have size, use 1
                                        'market_ticker': ticker_market
                                    }

                                    if self.on_trade_callback and trade_event['price']:
                                        self.on_trade_callback(trade_event)

                            elif msg_type == "subscribed":
                                print(f"✅ Subscription confirmed")

                        except json.JSONDecodeError as e:
                            print(f"❌ JSON decode error: {e}")

            except websockets.exceptions.WebSocketException as e:
                print(f"❌ WebSocket error: {e}")
                self.ws_connected = False

            except Exception as e:
                print(f"❌ Unexpected WebSocket error: {e}")
                self.ws_connected = False

        def run_async_ws():
            """Run async WebSocket in a thread."""
            self.ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.ws_loop)
            self.ws_loop.run_until_complete(ws_handler())

        # Run WebSocket in separate thread
        self.ws_thread = threading.Thread(target=run_async_ws, daemon=True)
        self.ws_thread.start()

        print(f"🚀 WebSocket thread started")

    def disconnect_websocket(self):
        """Disconnect from WebSocket."""
        self.ws_connected = False
        if self.ws_loop:
            self.ws_loop.stop()
        print(f"🔌 WebSocket disconnected")


# ============================================================================
# LIVE MARKET MAKER
# ============================================================================

class LiveMarketMaker:
    """
    Live market making bot with hybrid risk policy.

    Connects to Supabase, monitors trades, and executes market making strategy.
    """

    def __init__(self, config: BotConfig, market_ticker: str):
        self.config = config
        self.market_ticker = market_ticker

        # Kalshi API client
        self.kalshi_client = KalshiAPIClient(config)

        # Trading state
        self.inventory = InventoryState()
        self.policy = HybridRiskPolicy(config)

        # Price tracking
        self.mid_ema: Optional[float] = None

        # Logging
        self.fill_log: List[Dict] = []
        self.trigger_log: List[Dict] = []

        # Last processed trade
        self.last_trade_timestamp = 0

        # Statistics
        self.trades_processed = 0
        self.fills_executed = 0
        self.policy_triggers = 0

        # Live trading state
        self.active_bid_order: Optional[Dict] = None  # {'order_id': str, 'price': float, 'qty': int}
        self.active_ask_order: Optional[Dict] = None
        self.last_fill_check_time = 0
        self.live_trading_enabled = True  # Set to False to disable live orders

        # Connect to WebSocket for real-time data
        self.kalshi_client.connect_websocket(market_ticker, self.on_trade)

    def on_trade(self, trade: Dict):
        """
        Process incoming trade event.

        Args:
            trade: Dictionary with keys: timestamp, price, taker_side, count
        """
        timestamp = trade.get('timestamp', int(time.time()))
        price = trade.get('price')

        if not price:
            return

        price_dollars = price / 100

        # Update mid price EMA
        if self.mid_ema is None:
            self.mid_ema = price_dollars
        else:
            self.mid_ema = (self.config.MID_PRICE_EMA_ALPHA * price_dollars +
                           (1 - self.config.MID_PRICE_EMA_ALPHA) * self.mid_ema)

        # Check if policy says flatten
        should_flatten, reason = self.policy.should_flatten(
            self.inventory,
            self.mid_ema,
            timestamp
        )

        if should_flatten:
            self._flatten_inventory(self.mid_ema, timestamp, reason)
            # Cancel all orders when flattening
            if self.active_bid_order:
                self.kalshi_client.cancel_order(self.active_bid_order['order_id'])
                self.active_bid_order = None
            if self.active_ask_order:
                self.kalshi_client.cancel_order(self.active_ask_order['order_id'])
                self.active_ask_order = None
            return

        # Check for fills from the exchange periodically
        self._check_and_reconcile_fills()

        # Calculate inventory skew
        skew = self.policy.get_inventory_skew(
            self.inventory.net_contracts,
            size_cap=100
        )

        # Determine quotes based on inventory
        base_spread = self.config.BASE_SPREAD
        quote_size = self.config.SIZE_PER_FILL

        # Calculate quote prices based on inventory state
        quote_bid = None
        quote_ask = None

        # If neutral inventory (|net| < 20), quote both sides
        if abs(self.inventory.net_contracts) < 20:
            quote_bid = self.mid_ema - base_spread
            quote_ask = self.mid_ema + base_spread

        # If long inventory (net > 20), prefer to sell
        elif self.inventory.net_contracts > 20:
            quote_ask = self.mid_ema + base_spread * (1 + abs(skew))

        # If short inventory (net < -20), prefer to buy
        elif self.inventory.net_contracts < -20:
            quote_bid = self.mid_ema - base_spread * (1 + abs(skew))

        # Update orders on exchange
        self._update_quotes(quote_bid, quote_ask, quote_size)

        self.trades_processed += 1

    def _execute_fill(self, qty: int, price: float, timestamp: int, side: str):
        """Execute a new fill (open position)."""
        entry_fee = kalshi_fee(qty, price)
        self.inventory.add_fill(qty, price, timestamp, side)
        self.fills_executed += 1

        if self.config.LOG_FILLS:
            self.fill_log.append({
                'timestamp': timestamp,
                'qty': qty,
                'price': price,
                'side': side,
                'fee': entry_fee,
                'net_contracts_after': self.inventory.net_contracts,
                'vwap_after': self.inventory.vwap_entry
            })

        print(f"[FILL] {side.upper()} {qty} @ ${price:.4f} | Net: {self.inventory.net_contracts:+d} | VWAP: ${self.inventory.vwap_entry:.4f}")

    def _realize_position(self, qty: int, exit_price: float, timestamp: int, fees: float, reason: str):
        """Realize P&L on position."""
        realizations = self.inventory.realize_pnl(qty, exit_price, timestamp, fees)

        if self.config.LOG_REALIZATIONS:
            for r in realizations:
                r['exit_reason'] = reason
                r['remaining_net_contracts'] = self.inventory.net_contracts

        total_pnl = sum(r['pnl'] for r in realizations)
        print(f"[REALIZE] Closed {qty} @ ${exit_price:.4f} | P&L: ${total_pnl:+.2f} | Reason: {reason}")

    def _flatten_inventory(self, exit_price: float, timestamp: int, reason: str):
        """Flatten entire inventory (policy trigger)."""
        pnl_before = self.inventory.realized_pnl
        net_before = self.inventory.net_contracts
        vwap_before = self.inventory.vwap_entry

        realizations = self.inventory.flatten_all(exit_price, timestamp, fees=0.0)

        pnl_from_flatten = sum(r['pnl'] for r in realizations)

        if self.config.LOG_TRIGGERS:
            self.trigger_log.append({
                'timestamp': timestamp,
                'trigger_reason': reason,
                'net_contracts_before': net_before,
                'vwap_entry': vwap_before,
                'exit_price': exit_price,
                'pnl_before_flatten': pnl_before,
                'pnl_from_flatten': pnl_from_flatten,
                'pnl_after_flatten': self.inventory.realized_pnl
            })

        self.policy_triggers += 1

        print(f"[TRIGGER] {reason} | Flattened {net_before:+d} contracts @ ${exit_price:.4f} | P&L: ${pnl_from_flatten:+.2f}")

    def _send_discord_alert(self, message: str, embed: Optional[Dict] = None):
        """Send alert to Discord webhook."""
        webhook_url = self.config.DISCORD_WEBHOOK_URL
        if not webhook_url:
            return

        try:
            payload = {"content": message}
            if embed:
                payload["embeds"] = [embed]

            response = self.kalshi_client.session.post(webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"⚠️  Failed to send Discord alert: {e}")

    def _update_quotes(self, bid_price: Optional[float], ask_price: Optional[float], qty: int):
        """
        Update active quotes on the exchange.
        Cancels old orders and places new ones if prices changed.
        """
        if not self.live_trading_enabled:
            return

        # Handle bid side
        if bid_price is not None:
            bid_cents = int(bid_price * 100)

            # Cancel old bid if price changed or doesn't exist
            if self.active_bid_order:
                old_price_cents = int(self.active_bid_order['price'] * 100)
                if old_price_cents != bid_cents:
                    self.kalshi_client.cancel_order(self.active_bid_order['order_id'])
                    self.active_bid_order = None

            # Place new bid if needed
            if not self.active_bid_order:
                order = self.kalshi_client.place_order(
                    market_ticker=self.market_ticker,
                    side="yes",
                    action="buy",
                    count=qty,
                    price_cents=bid_cents
                )
                if order:
                    self.active_bid_order = {
                        'order_id': order.get('order', {}).get('order_id'),
                        'price': bid_price,
                        'qty': qty
                    }
                    self._send_discord_alert(f"📊 **Order Placed**\nBID: {qty} @ ${bid_price:.4f} ({bid_cents}¢)")
        else:
            # Cancel bid if no longer needed
            if self.active_bid_order:
                self.kalshi_client.cancel_order(self.active_bid_order['order_id'])
                self.active_bid_order = None

        # Handle ask side
        if ask_price is not None:
            ask_cents = int(ask_price * 100)

            # Cancel old ask if price changed or doesn't exist
            if self.active_ask_order:
                old_price_cents = int(self.active_ask_order['price'] * 100)
                if old_price_cents != ask_cents:
                    self.kalshi_client.cancel_order(self.active_ask_order['order_id'])
                    self.active_ask_order = None

            # Place new ask if needed
            if not self.active_ask_order:
                order = self.kalshi_client.place_order(
                    market_ticker=self.market_ticker,
                    side="yes",
                    action="sell",
                    count=qty,
                    price_cents=ask_cents
                )
                if order:
                    self.active_ask_order = {
                        'order_id': order.get('order', {}).get('order_id'),
                        'price': ask_price,
                        'qty': qty
                    }
                    self._send_discord_alert(f"📊 **Order Placed**\nASK: {qty} @ ${ask_price:.4f} ({ask_cents}¢)")
        else:
            # Cancel ask if no longer needed
            if self.active_ask_order:
                self.kalshi_client.cancel_order(self.active_ask_order['order_id'])
                self.active_ask_order = None

    def _check_and_reconcile_fills(self):
        """
        Check for actual fills from the exchange and reconcile with local state.
        Should be called periodically (every few seconds).
        """
        current_time = int(time.time())

        # Only check every 3 seconds
        if current_time - self.last_fill_check_time < 3:
            return

        self.last_fill_check_time = current_time

        # Fetch recent fills for this market
        fills = self.kalshi_client.get_fills(ticker=self.market_ticker, limit=50)

        for fill in fills:
            fill_time = fill.get('created_time')
            # Only process recent fills (within last 10 seconds)
            # This prevents reprocessing old fills on startup
            if fill_time and current_time - fill_time <= 10:
                self._process_exchange_fill(fill)

    def _process_exchange_fill(self, fill: Dict):
        """Process a fill received from the exchange API."""
        try:
            # Extract fill details
            side = fill.get('side')  # 'yes' or 'no'
            action = fill.get('action')  # 'buy' or 'sell'
            count = fill.get('count', 0)
            price_cents = fill.get('yes_price') if side == 'yes' else fill.get('no_price')

            if not all([side, action, count, price_cents]):
                return

            price = price_cents / 100
            timestamp = fill.get('created_time', int(time.time()))

            # Determine if this is opening or closing a position
            # Buy yes = long, Sell yes = close long
            # Buy no = short, Sell no = close short
            if action == 'buy' and side == 'yes':
                # Opening long position
                self._execute_fill(count, price, timestamp, 'long')
            elif action == 'sell' and side == 'yes':
                # Closing long position
                fees = fees_roundtrip(count, self.inventory.vwap_entry, price)
                self._realize_position(count, price, timestamp, fees, 'fill_from_exchange')
            elif action == 'buy' and side == 'no':
                # Opening short position
                self._execute_fill(count, price, timestamp, 'short')
            elif action == 'sell' and side == 'no':
                # Closing short position
                fees = fees_roundtrip(count, abs(self.inventory.vwap_entry), price)
                self._realize_position(count, price, timestamp, fees, 'fill_from_exchange')

            # Send Discord alert for fill
            emoji = "🟢" if action == "buy" else "🔴"
            self._send_discord_alert(
                f"{emoji} **FILL RECEIVED**\n"
                f"{action.upper()} {side.upper()} {count} @ ${price:.4f}\n"
                f"Net Position: {self.inventory.net_contracts:+d}"
            )

        except Exception as e:
            print(f"⚠️  Error processing exchange fill: {e}")

    def get_status(self) -> Dict:
        """Get current bot status."""
        return {
            'market_ticker': self.market_ticker,
            'net_contracts': self.inventory.net_contracts,
            'vwap_entry': self.inventory.vwap_entry,
            'realized_pnl': self.inventory.realized_pnl,
            'mid_ema': self.mid_ema,
            'trades_processed': self.trades_processed,
            'fills_executed': self.fills_executed,
            'policy_triggers': self.policy_triggers,
            'duration_weighted_exposure': self.inventory.duration_weighted_exposure(get_timestamp()),
            'inventory_mae': self.inventory.inventory_mae(self.mid_ema) if self.mid_ema else 0.0
        }

    def export_logs(self, output_dir: str = 'logs'):
        """Export trading logs to CSV files."""
        Path(output_dir).mkdir(exist_ok=True)

        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        ticker_clean = self.market_ticker.replace('-', '_')

        # Export fills
        if self.fill_log:
            import csv
            fill_file = f"{output_dir}/{ticker_clean}_fills_{timestamp_str}.csv"
            with open(fill_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.fill_log[0].keys())
                writer.writeheader()
                writer.writerows(self.fill_log)
            print(f"✅ Exported {len(self.fill_log)} fills to {fill_file}")

        # Export realizations
        if self.inventory.all_realizations:
            import csv
            real_file = f"{output_dir}/{ticker_clean}_realizations_{timestamp_str}.csv"
            with open(real_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.inventory.all_realizations[0].keys())
                writer.writeheader()
                writer.writerows(self.inventory.all_realizations)
            print(f"✅ Exported {len(self.inventory.all_realizations)} realizations to {real_file}")

        # Export triggers
        if self.trigger_log:
            import csv
            trigger_file = f"{output_dir}/{ticker_clean}_triggers_{timestamp_str}.csv"
            with open(trigger_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.trigger_log[0].keys())
                writer.writeheader()
                writer.writerows(self.trigger_log)
            print(f"✅ Exported {len(self.trigger_log)} triggers to {trigger_file}")


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    """Main bot execution."""
    import os
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv()

    # Validate settings
    try:
        settings.validate()
    except ValueError as e:
        print(f"❌ Configuration Error: {e}")
        print("\nPlease set the required environment variables:")
        print("  - KALSHI_API_KEY (your email)")
        print("  - KALSHI_API_SECRET (your RSA private key)")
        print("\nSee .env.example for reference.")
        return

    config = BotConfig()

    print("="*80)
    print("LIVE MARKET MAKING BOT - NHL EDITION")
    print("="*80)
    print(f"Series: {config.SERIES_TICKER}")
    print(f"Base URL: {config.KALSHI_BASE_URL}")
    print(f"Policy: Duration-Weighted (limit={config.DURATION_WEIGHTED_LIMIT}) + MAE Failsafe ({config.MAE_FAILSAFE_CENTS}¢, {config.MAE_ACTIVATION_WINDOW_SEC}s)")
    print(f"Parameters: {config.SIZE_PER_FILL} contracts/fill, ${config.MAX_INVENTORY_VALUE} max inventory")
    print("="*80)
    print()

    # Auto-discover markets or use manual override
    market_ticker = os.getenv('MARKET_TICKER')

    if not market_ticker or market_ticker == 'auto':
        print("🔍 Auto-discovering active markets...")

        # Create temporary API client for market discovery
        temp_client = KalshiAPIClient(config)
        markets = temp_client.get_active_markets(series_ticker=config.SERIES_TICKER, limit=100)

        if not markets:
            print("❌ No active markets found!")
            return

        # Filter for today's games by parsing ticker date
        # Ticker format: KXNHLGAME-25OCT27... or KXNHLGAME-25OCT28...
        from datetime import datetime
        today = datetime.now()
        today_str = today.strftime("%y%b%d").upper()  # e.g., "25OCT27"

        print(f"🗓️  Filtering for today's date: {today_str}")

        markets_today = []
        for m in markets:
            ticker = m.get('ticker', '')
            if today_str in ticker:
                markets_today.append(m)

        if not markets_today:
            print(f"❌ No markets found for today ({today_str})!")
            print(f"   Available dates in tickers:")
            for m in markets[:5]:
                print(f"   - {m.get('ticker', 'N/A')}")
            return

        print(f"🕐 Filtered to {len(markets_today)} markets for today")

        # Sort by volume (most active first)
        markets_sorted = sorted(markets_today, key=lambda x: x.get('volume', 0), reverse=True)

        print(f"\n📊 Top {min(5, len(markets_sorted))} Active Markets:")
        for i, m in enumerate(markets_sorted[:5]):
            print(f"  {i+1}. {m.get('ticker')} - Vol: {m.get('volume', 0)}")

        # Use most active market
        market_ticker = markets_sorted[0]['ticker']
        print(f"\n✅ Selected most active market: {market_ticker}")
        print(f"   Volume: {markets_sorted[0].get('volume', 0)}")
        print(f"   Title: {markets_sorted[0].get('title', 'N/A')}")
        print()
    else:
        print(f"📍 Using manual market: {market_ticker}\n")

    # Initialize bot
    try:
        bot = LiveMarketMaker(config, market_ticker)

        print("✅ Bot initialized. Ready to trade.")
        print("Status:", json.dumps(bot.get_status(), indent=2))
        print()
        print("🚀 Bot is now running and listening for trades...")
        print("   Press Ctrl+C to stop")
        print()

        # Keep main thread alive
        while True:
            time.sleep(10)

            # Periodically print status
            status = bot.get_status()
            print(f"[STATUS] Net: {status['net_contracts']:+d} | P&L: ${status['realized_pnl']:+.2f} | " +
                  f"Trades: {status['trades_processed']} | Fills: {status['fills_executed']} | " +
                  f"Triggers: {status['policy_triggers']}")

    except KeyboardInterrupt:
        print("\n⚠️ Shutting down bot...")
        bot.kalshi_client.disconnect_websocket()
        bot.export_logs()
        print("✅ Bot shutdown complete")

    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        raise


if __name__ == '__main__':
    main()
