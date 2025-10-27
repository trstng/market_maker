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

                            # DEBUG: Log all message types
                            print(f"🔍 WS Message: type={msg_type}, keys={list(data.keys())}")

                            if msg_type == "trade":
                                # Process trade event (actual trade execution)
                                trade_data = data.get("msg", {})
                                print(f"💰 TRADE: {trade_data}")
                                if self.on_trade_callback:
                                    self.on_trade_callback(trade_data)

                            elif msg_type == "ticker":
                                # Process ticker update (price changes)
                                ticker_data = data.get("msg", {})
                                ticker_market = ticker_data.get("market_ticker", "unknown")

                                # Only process if it's our market
                                if ticker_market.lower() == market_ticker.lower():
                                    print(f"📊 TICKER UPDATE: market={ticker_market}, price={ticker_data.get('price')}, yes_bid={ticker_data.get('yes_bid')}, yes_ask={ticker_data.get('yes_ask')}")

                                    # Convert ticker to trade-like event for bot processing
                                    # Use timestamp and price from ticker
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
                                print(f"✅ Subscription confirmed: {data}")

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

        # Connect to WebSocket for real-time data
        self.kalshi_client.connect_websocket(market_ticker, self.on_trade)

    def on_trade(self, trade: Dict):
        """
        Process incoming trade event.

        Args:
            trade: Dictionary with keys: timestamp, price, taker_side, count
        """
        timestamp = trade['timestamp']
        price = trade.get('price')
        taker_side = trade.get('taker_side')
        trade_size = trade.get('count', 10)

        if not price or not taker_side:
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
            return

        # Calculate inventory skew
        skew = self.policy.get_inventory_skew(
            self.inventory.net_contracts,
            size_cap=100
        )

        # Determine if we quote and get filled
        base_spread = self.config.BASE_SPREAD
        my_queue_share = self.config.QUEUE_SHARE

        # If neutral inventory (|net| < 20), quote both sides
        if abs(self.inventory.net_contracts) < 20:
            quote_bid = self.mid_ema - base_spread
            quote_ask = self.mid_ema + base_spread

            # Seller ("no" taker) hits our bid if trade_price <= bid
            if taker_side == 'no' and price_dollars <= quote_bid:
                # We provide bid liquidity (buy at our bid)
                filled_qty = min(self.config.SIZE_PER_FILL, int(my_queue_share * trade_size))
                if filled_qty > 0:
                    self._execute_fill(filled_qty, quote_bid, timestamp, 'long')

            # Buyer ("yes" taker) lifts our ask if trade_price >= ask
            elif taker_side == 'yes' and price_dollars >= quote_ask:
                # We provide ask liquidity (sell at our ask)
                filled_qty = min(self.config.SIZE_PER_FILL, int(my_queue_share * trade_size))
                if filled_qty > 0:
                    self._execute_fill(filled_qty, quote_ask, timestamp, 'short')

        # If long inventory (net > 20), prefer to sell
        elif self.inventory.net_contracts > 20:
            quote_ask = self.mid_ema + base_spread * (1 + abs(skew))

            # Buyer hits our ask if trade_price >= ask
            if taker_side == 'yes' and price_dollars >= quote_ask:
                filled_qty = min(self.config.SIZE_PER_FILL, int(my_queue_share * trade_size))
                if filled_qty > 0:
                    # Close position
                    roundtrip_fees = fees_roundtrip(filled_qty, self.inventory.vwap_entry, quote_ask)
                    self._realize_position(filled_qty, quote_ask, timestamp, roundtrip_fees, 'normal_exit')

        # If short inventory (net < -20), prefer to buy
        elif self.inventory.net_contracts < -20:
            quote_bid = self.mid_ema - base_spread * (1 + abs(skew))

            # Seller hits our bid if trade_price <= bid
            if taker_side == 'no' and price_dollars <= quote_bid:
                filled_qty = min(self.config.SIZE_PER_FILL, int(my_queue_share * trade_size))
                if filled_qty > 0:
                    # Close position
                    roundtrip_fees = fees_roundtrip(filled_qty, abs(self.inventory.vwap_entry), quote_bid)
                    self._realize_position(filled_qty, quote_bid, timestamp, roundtrip_fees, 'normal_exit')

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

    # Get market ticker from environment or use example
    market_ticker = os.getenv('MARKET_TICKER', 'KXNHLGAME-25OCT30EXAMPLE-TEAM')

    print("="*80)
    print("LIVE MARKET MAKING BOT - NHL EDITION")
    print("="*80)
    print(f"Market: {market_ticker}")
    print(f"Series: {config.SERIES_TICKER}")
    print(f"Base URL: {config.KALSHI_BASE_URL}")
    print(f"Policy: Duration-Weighted (limit={config.DURATION_WEIGHTED_LIMIT}) + MAE Failsafe ({config.MAE_FAILSAFE_CENTS}¢, {config.MAE_ACTIVATION_WINDOW_SEC}s)")
    print(f"Parameters: {config.SIZE_PER_FILL} contracts/fill, ${config.MAX_INVENTORY_VALUE} max inventory")
    print("="*80)
    print()

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
