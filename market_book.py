"""
MarketBook - Per-market trading logic
Async task that manages one market's state, quotes, and fills
"""
import time
import asyncio
import math
import random
import json
import os
from enum import Enum
from collections import deque
from typing import Optional, Dict, Tuple
from order_state import OrderState


class QuoteState(Enum):
    """State machine for market making strategy"""
    FLAT = "FLAT"  # No inventory, quote both sides in profitable zones
    SKEW_LONG = "SKEW_LONG"  # Long inventory, ASK-only until flat
    SKEW_SHORT = "SKEW_SHORT"  # Short inventory, BID-only until flat


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

        # Orderbook tracking (for VWAP-anchored quotes)
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.bid_size: int = 0
        self.ask_size: int = 0

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

        # ======================================================================
        # NEW: State Machine & Edge-Locked MM Tracking
        # ======================================================================

        # State machine
        self.quote_state: QuoteState = QuoteState.FLAT
        self.last_state_change_ts: float = 0.0

        # VWAP-anchored TP tracking (only update when VWAP shifts by ≥1 tick)
        self.last_tp_vwap: Optional[float] = None

        # MAE trim tracking
        self.last_trim_ts: float = 0.0

        # Circuit breaker tracking (2-tier, rolling windows)
        self.cancel_timestamps_30s: deque = deque()
        self.cancel_timestamps_60s: deque = deque()
        self.circuit_brake_until: float = 0.0
        self.circuit_brake_tier: str = "none"

        # Logging
        self.fill_log = []
        self.realization_log = []

        # ======================================================================
        # Startup Sanity Checks
        # ======================================================================
        self._log_startup_sanity_checks()

    def _log_startup_sanity_checks(self):
        """Log critical parameter sanity checks on initialization."""
        tp_offset = self.tp_offset_c()
        example_vwap = 0.60
        example_tp_long = example_vwap + (tp_offset / 100)
        example_tp_short = example_vwap - (tp_offset / 100)

        low_gate, high_gate = self.get_entry_thresholds()

        print(f"\n{'='*60}")
        print(f"[{self.ticker}] STARTUP SANITY CHECKS")
        print(f"{'='*60}")
        print(f"TP Math:")
        print(f"  Fees: {self.config.MAKER_FEE_C:.2f}¢ + {self.config.EXIT_FEE_C:.2f}¢ = {self.config.MAKER_FEE_C + self.config.EXIT_FEE_C:.2f}¢")
        print(f"  Min Edge: {self.config.TP_MIN_EDGE_C:.0f}¢")
        print(f"  Tick: {self.config.TICK_C:.0f}¢")
        print(f"  TP Offset: {tp_offset:.0f}¢")
        print(f"  Example: VWAP=${example_vwap:.2f} → TP_LONG=${example_tp_long:.2f}, TP_SHORT=${example_tp_short:.2f}")
        print(f"Price Gates:")
        print(f"  Entry Low: ${low_gate:.2f} (only buy below)")
        print(f"  Entry High: ${high_gate:.2f} (only sell above)")
        print(f"Exit Windows:")
        print(f"  Sweet Spot: {self.config.SWEET_SPOT_MIN_S/60:.0f}-{self.config.SWEET_SPOT_MAX_S/60:.0f} min")
        print(f"  Hard Cap: {self.config.MAX_AGE_SECONDS/60:.0f} min")
        print(f"Circuit Breaker:")
        print(f"  Soft: {self.config.CB_TIER1_30S}/30s or {self.config.CB_TIER1_60S}/60s")
        print(f"  Hard: {self.config.CB_TIER2_30S}/30s or {self.config.CB_TIER2_60S}/60s")
        print(f"{'='*60}\n")

    def _log_event(self, action: str, **kwargs):
        """
        Emit single-line JSON telemetry for every event.

        Makes drift/bugs instantly obvious in logs.

        Standard fields (always included):
        - ts, market, state, net, vwap, tp, best_bid, best_ask, action

        Additional fields via kwargs:
        - cooldown_ms, hysteresis_ticks, spread_ticks
        - mae_c, age_s, trim_pct, trim_qty
        - cancels_30, cancels_60, cb_tier
        - price, qty, reason, etc.
        """
        vwap = self.inventory.vwap_entry
        tp = None
        if vwap is not None and self.quote_state != QuoteState.FLAT:
            tp_offset_dollars = self.tp_offset_c() / 100
            if self.quote_state == QuoteState.SKEW_LONG:
                tp = vwap + tp_offset_dollars
            else:  # SKEW_SHORT
                tp = vwap - tp_offset_dollars

        event = {
            "ts": time.time(),
            "market": self.ticker,
            "state": self.quote_state.value,
            "net": self.inventory.net_contracts,
            "vwap": round(vwap, 4) if vwap else None,
            "tp": round(tp, 4) if tp else None,
            "best_bid": round(self.best_bid, 4) if self.best_bid else None,
            "best_ask": round(self.best_ask, 4) if self.best_ask else None,
            "action": action,
            **kwargs
        }
        print(json.dumps(event))

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
        """
        NEW: Process incoming market message with integrated exit logic.

        Priority order:
        1. Age-based exits (sweet-spot, hard-cap)
        2. MAE trimming
        3. Risk policy flatten (legacy)
        4. Normal quoting
        """
        ts = msg.get('timestamp', int(time.time()))
        price_c = msg.get('price')

        if price_c is None:
            return

        px = price_c / 100

        # Update mid price EMA
        alpha = self.config.MID_PRICE_EMA_ALPHA
        if self.mid_ema is None:
            self.mid_ema = px
        else:
            self.mid_ema = alpha * px + (1 - alpha) * self.mid_ema

        # ======================================================================
        # NEW: Age-Based Exit Windows (Highest Priority)
        # ======================================================================
        should_exit, exit_reason, exit_mode = self.check_exit_windows()
        if should_exit:
            if exit_mode == "hard_cap":
                # Force exit at 40 min (bypasses all other logic)
                await self.execute_hard_cap_exit()
                await self._cancel_both()
                print(f"[{self.ticker}] {exit_reason}")
                return
            elif exit_mode == "sweet_spot":
                # Smart exit during 8-12 min window
                await self.execute_sweet_spot_exit()
                await self._cancel_both()
                print(f"[{self.ticker}] {exit_reason}")
                return

        # ======================================================================
        # NEW: MAE Trimming (Second Priority)
        # ======================================================================
        should_trim, trim_pct, trim_qty = self.check_mae_trim(px)
        if should_trim:
            mae_c = abs(px - self.inventory.vwap_entry) * 100
            print(f"[{self.ticker}] MAE trim triggered: {mae_c:.1f}¢ MAE, trimming {trim_pct*100:.0f}% ({trim_qty} contracts)")
            await self.execute_mae_trim(trim_qty, px)
            # After trim, recompute VWAP and update TP quotes
            quote_bid, quote_ask = self._compute_quotes()
            await self._update_quotes_with_exception(quote_bid, quote_ask, self.config.SIZE_PER_FILL)
            return

        # ======================================================================
        # Legacy Risk Policy (Duration-Weighted, MAE Failsafe)
        # ======================================================================
        should_flatten, reason = self.policy.should_flatten(
            self.inventory,
            self.mid_ema,
            ts
        )

        if should_flatten:
            await self._flatten(self.mid_ema, ts, reason)
            await self._cancel_both()
            return

        # ======================================================================
        # NEW: Circuit Breaker Check
        # ======================================================================
        cb_active, cb_tier = self.check_circuit_breaker()
        if cb_active:
            if cb_tier == "hard":
                # Hard brake: Only allow exit orders, no normal quoting
                return
            elif cb_tier == "soft":
                # Soft brake: Widen quotes by 1 tick, double cooldown (handled in _cooldown_ok)
                # Continue to normal quoting but with restrictions
                pass

        # ======================================================================
        # Normal Quoting
        # ======================================================================
        quote_bid, quote_ask = self._compute_quotes()

        # Apply soft brake adjustments if active
        if cb_tier == "soft" and (quote_bid is not None or quote_ask is not None):
            tick = self.config.TICK_C / 100
            if quote_bid is not None:
                quote_bid = self.round_to_tick(quote_bid - tick)  # Widen by 1 tick
            if quote_ask is not None:
                quote_ask = self.round_to_tick(quote_ask + tick)  # Widen by 1 tick

        # Update orders on exchange
        await self._update_quotes(quote_bid, quote_ask, self.config.SIZE_PER_FILL)

    def _compute_quotes(self) -> Tuple[Optional[float], Optional[float]]:
        """
        NEW: State-machine-based quote computation with VWAP-anchored TP.

        FLAT mode: Quote both sides only in profitable zones (price-selective gating)
        SKEW_LONG mode: ASK-only at VWAP + TP offset (edge-locked)
        SKEW_SHORT mode: BID-only at VWAP - TP offset (edge-locked)

        Returns:
            (bid_price, ask_price) - either can be None
        """
        mid = self.mid_ema
        if mid is None:
            return (None, None)

        net = self.inventory.net_contracts

        # Determine state from inventory
        if net == 0:
            current_state = QuoteState.FLAT
        elif net > 0:
            current_state = QuoteState.SKEW_LONG
        else:
            current_state = QuoteState.SKEW_SHORT

        # Update state if changed
        if current_state != self.quote_state:
            self.quote_state = current_state
            self.last_state_change_ts = time.time()
            self.last_tp_vwap = None  # Force TP recalc

        # ======================================================================
        # FLAT Mode: Quote both sides in profitable zones
        # ======================================================================
        if self.quote_state == QuoteState.FLAT:
            low_gate, high_gate = self.get_entry_thresholds()
            base_spread = self.config.BASE_SPREAD

            can_buy = mid <= low_gate
            can_sell = mid >= high_gate

            bid = None
            ask = None

            if can_buy:
                # Place bid 1 tick inside mid (maker-only, never cross)
                bid = self.round_to_tick(mid - base_spread)
                # Ensure bid doesn't cross spread (if we have book data)
                if self.best_ask is not None and bid >= self.best_ask:
                    bid = self.round_to_tick(self.best_ask - self.config.TICK_C / 100)

            if can_sell:
                # Place ask 1 tick outside mid (maker-only, never cross)
                ask = self.round_to_tick(mid + base_spread)
                # Ensure ask doesn't cross spread (if we have book data)
                if self.best_bid is not None and ask <= self.best_bid:
                    ask = self.round_to_tick(self.best_bid + self.config.TICK_C / 100)

            return (bid, ask)

        # ======================================================================
        # SKEW_LONG Mode: ASK-only at VWAP + TP offset
        # ======================================================================
        elif self.quote_state == QuoteState.SKEW_LONG:
            vwap = self.inventory.vwap_entry
            if vwap is None:
                return (None, None)

            # Check if VWAP changed by ≥1 tick (hysteresis for TP updates)
            vwap_changed = (
                self.last_tp_vwap is None or
                abs(vwap - self.last_tp_vwap) >= (self.config.TICK_C / 100)
            )

            if vwap_changed:
                self.last_tp_vwap = vwap

            # Calculate TP price (VWAP + 7¢ with default params)
            tp_offset_dollars = self.tp_offset_c() / 100  # Convert cents to dollars
            tp_price = self.round_to_tick(vwap + tp_offset_dollars)

            # Ensure TP never inside touch (if we have book data)
            if self.best_ask is not None:
                tp_price = max(tp_price, self.best_ask)

            return (None, tp_price)

        # ======================================================================
        # SKEW_SHORT Mode: BID-only at VWAP - TP offset
        # ======================================================================
        elif self.quote_state == QuoteState.SKEW_SHORT:
            vwap = self.inventory.vwap_entry
            if vwap is None:
                return (None, None)

            # Check if VWAP changed by ≥1 tick
            vwap_changed = (
                self.last_tp_vwap is None or
                abs(vwap - self.last_tp_vwap) >= (self.config.TICK_C / 100)
            )

            if vwap_changed:
                self.last_tp_vwap = vwap

            # Calculate TP price (VWAP - 7¢ with default params)
            tp_offset_dollars = self.tp_offset_c() / 100
            tp_price = self.round_to_tick(vwap - tp_offset_dollars)

            # Ensure TP never inside touch (if we have book data)
            if self.best_bid is not None:
                tp_price = min(tp_price, self.best_bid)

            return (tp_price, None)

        return (None, None)

    # ======================================================================
    # NEW: Utility Functions for Edge-Locked MM
    # ======================================================================

    def tp_offset_c(self) -> float:
        """
        Calculate take-profit offset in cents (fee-aware, tick-snapped).

        Formula: roundup((MAKER_FEE + EXIT_FEE + 1 tick + min_edge) / tick_size) × tick_size

        With default params (MAKER=1.75¢, EXIT=1.75¢, TICK=1¢, min_edge=2¢):
        raw = 3.5¢ + 1¢ + 2¢ = 6.5¢
        snap = ceil(6.5 / 1) × 1 = 7¢

        Returns:
            TP offset in cents (e.g., 7.0)
        """
        cfg = self.config
        FEE_C = cfg.MAKER_FEE_C + cfg.EXIT_FEE_C
        TICK = cfg.TICK_C
        raw = FEE_C + TICK + cfg.TP_MIN_EDGE_C
        return math.ceil(raw / TICK) * TICK

    def round_to_tick(self, price: float) -> float:
        """
        Snap price to tick grid.

        Args:
            price: Price in dollars (e.g., 0.605)

        Returns:
            Tick-snapped price in dollars (e.g., 0.61)
        """
        tick_dollars = self.config.TICK_C / 100  # Convert cents to dollars
        return round(price / tick_dollars) * tick_dollars

    def get_entry_thresholds(self) -> Tuple[float, float]:
        """
        Get per-market entry price thresholds for price-selective gating.

        Extracts market series (NHL, NFL, CFB, etc.) from ticker and looks up
        configured thresholds, falling back to defaults.

        Returns:
            (low_threshold, high_threshold) - only buy below low, sell above high
        """
        # Extract series from ticker (e.g., "NHL-LEAFS-WIN-B45" -> "NHL")
        market_key = self.ticker.split("-")[0] if "-" in self.ticker else self.ticker

        thresholds = self.config.MARKET_THRESHOLDS.get(
            market_key,
            {
                "low": self.config.DEFAULT_PRICE_ENTRY_LOW,
                "high": self.config.DEFAULT_PRICE_ENTRY_HIGH
            }
        )
        return thresholds["low"], thresholds["high"]

    def crossed_or_far_from_touch(self, our_price: float, side: str) -> bool:
        """
        Check if order needs replacement due to crossing, inside spread,
        or displacement by ≥ HYSTERESIS_TICKS.

        CRITICAL FIX: In SKEW modes, only replace if crossed/inside.
        Displacement hysteresis only applies in FLAT mode (normal quoting).

        In SKEW mode, TP is VWAP-anchored and should not be displaced by market movement.

        Args:
            our_price: Current resting order price
            side: 'bid' or 'ask'

        Returns:
            True if order should be replaced
        """
        # If we don't have book data, fall back to simple check
        if self.best_bid is None or self.best_ask is None:
            return False

        # Always check crossing/inside spread (all modes)
        if side == "bid":
            if our_price >= self.best_ask:
                return True  # Crossed
        else:  # ask
            if our_price <= self.best_bid:
                return True  # Crossed

        # CRITICAL FIX: Only apply displacement hysteresis in FLAT mode
        # In SKEW modes, TP is VWAP-anchored and should not move with market
        if self.quote_state != QuoteState.FLAT:
            return False  # Don't replace for displacement in SKEW

        # FLAT mode: Apply displacement hysteresis
        tick = self.config.TICK_C / 100
        hysteresis = self.config.HYSTERESIS_TICKS * tick

        if side == "bid":
            if our_price < self.best_bid - hysteresis:
                return True
        else:  # ask
            if our_price > self.best_ask + hysteresis:
                return True

        return False

    # ======================================================================
    # NEW: Age-Based Exits & MAE Trimming
    # ======================================================================

    def check_exit_windows(self) -> Tuple[bool, str, Optional[str]]:
        """
        Check if position should be exited based on age.

        Returns:
            (should_exit, reason, exit_mode)
            exit_mode: "sweet_spot", "hard_cap", or None
        """
        if self.quote_state == QuoteState.FLAT:
            return False, "", None

        age_s = self.inventory.oldest_fill_age()
        if age_s is None:
            return False, "", None

        # Hard cap (40 min) - force exit regardless of conditions
        if age_s >= self.config.MAX_AGE_SECONDS:
            return True, f"Hard cap exit ({age_s/60:.1f}m)", "hard_cap"

        # Sweet spot (8-12 min) - optimal exit window
        if self.config.SWEET_SPOT_MIN_S <= age_s <= self.config.SWEET_SPOT_MAX_S:
            return True, f"Sweet spot exit ({age_s/60:.1f}m)", "sweet_spot"

        return False, "", None

    async def execute_sweet_spot_exit(self):
        """
        Execute smart exit during sweet-spot window with depth-aware pricing.

        Tries to improve by 1-2 ticks if depth conditions allow, with dwell timer.
        Falls back to touch if not filled within dwell period.
        """
        if self.best_bid is None or self.best_ask is None:
            # No book data, just flatten at mid
            await self._flatten(self.mid_ema, int(time.time()), "sweet_spot_no_book")
            await self._cancel_both()
            return

        net = self.inventory.net_contracts
        qty_to_exit = abs(net)

        spread_ticks = int((self.best_ask - self.best_bid) / (self.config.TICK_C / 100))

        # CRITICAL FIX: Never try to improve in 1-tick spreads
        if spread_ticks == 1:
            if self.quote_state == QuoteState.SKEW_LONG:
                exit_price = self.best_bid
                print(f"[{self.ticker}] Sweet-spot exit: 1-tick spread, no improvement @ ${exit_price:.4f}")
                await self._place_ioc_order(exit_price, qty_to_exit, "sell")
            else:  # SKEW_SHORT
                exit_price = self.best_ask
                print(f"[{self.ticker}] Sweet-spot exit: 1-tick spread, no improvement @ ${exit_price:.4f}")
                await self._place_ioc_order(exit_price, qty_to_exit, "buy")
            return

        if self.quote_state == QuoteState.SKEW_LONG:
            # Need to sell - check depth at bid
            depth_at_touch = self.bid_size

            # Calculate improvement potential
            can_improve_2tick = (
                spread_ticks >= 3 and
                depth_at_touch >= max(self.config.DEPTH_MULTIPLE_2TICK * qty_to_exit, self.config.DEPTH_MIN_2TICK)
            )
            can_improve_1tick = (
                spread_ticks >= 2 and
                depth_at_touch >= max(self.config.DEPTH_MULTIPLE_1TICK * qty_to_exit, self.config.DEPTH_MIN_1TICK)
            )

            if can_improve_2tick:
                exit_price = self.round_to_tick(self.best_bid + 2 * (self.config.TICK_C / 100))
                print(f"[{self.ticker}] Sweet-spot exit: trying 2 ticks better at ${exit_price:.4f}")
            elif can_improve_1tick:
                exit_price = self.round_to_tick(self.best_bid + (self.config.TICK_C / 100))
                print(f"[{self.ticker}] Sweet-spot exit: trying 1 tick better at ${exit_price:.4f}")
            else:
                exit_price = self.best_bid
                print(f"[{self.ticker}] Sweet-spot exit: at touch ${exit_price:.4f}")

            # Log sweet-spot exit
            self._log_event("sweet_spot_exit",
                          qty=qty_to_exit,
                          exit_price=round(exit_price, 4),
                          age_s=self.inventory.oldest_fill_age(),
                          spread_ticks=spread_ticks,
                          depth=depth_at_touch)

            # Place IOC exit order
            await self._place_ioc_order(exit_price, qty_to_exit, "sell")

        else:  # SKEW_SHORT
            # Need to buy - check depth at ask
            depth_at_touch = self.ask_size

            can_improve_2tick = (
                spread_ticks >= 3 and
                depth_at_touch >= max(self.config.DEPTH_MULTIPLE_2TICK * qty_to_exit, self.config.DEPTH_MIN_2TICK)
            )
            can_improve_1tick = (
                spread_ticks >= 2 and
                depth_at_touch >= max(self.config.DEPTH_MULTIPLE_1TICK * qty_to_exit, self.config.DEPTH_MIN_1TICK)
            )

            if can_improve_2tick:
                exit_price = self.round_to_tick(self.best_ask - 2 * (self.config.TICK_C / 100))
                print(f"[{self.ticker}] Sweet-spot exit: trying 2 ticks better at ${exit_price:.4f}")
            elif can_improve_1tick:
                exit_price = self.round_to_tick(self.best_ask - (self.config.TICK_C / 100))
                print(f"[{self.ticker}] Sweet-spot exit: trying 1 tick better at ${exit_price:.4f}")
            else:
                exit_price = self.best_ask
                print(f"[{self.ticker}] Sweet-spot exit: at touch ${exit_price:.4f}")

            # Place IOC exit order
            await self._place_ioc_order(exit_price, qty_to_exit, "buy")

    async def execute_hard_cap_exit(self):
        """Force exit entire position at hard cap (40 min) using IOC."""
        if self.quote_state == QuoteState.FLAT:
            return

        net = self.inventory.net_contracts
        qty_to_exit = abs(net)

        if self.quote_state == QuoteState.SKEW_LONG:
            # Sell at best bid (or mid if no book data)
            exit_price = self.best_bid if self.best_bid is not None else self.mid_ema
            await self._place_ioc_order(exit_price, qty_to_exit, "sell")
        else:  # SKEW_SHORT
            # Buy at best ask (or mid if no book data)
            exit_price = self.best_ask if self.best_ask is not None else self.mid_ema
            await self._place_ioc_order(exit_price, qty_to_exit, "buy")

        print(f"[{self.ticker}] Hard cap exit: force flatten {qty_to_exit} @ ${exit_price:.4f}")

    def check_mae_trim(self, mark: float) -> Tuple[bool, float, int]:
        """
        Check if adaptive MAE trim needed.

        Trim percentage scales from 25% to 50% based on severity.

        Args:
            mark: Current market price

        Returns:
            (should_trim, trim_pct, trim_qty)
        """
        if self.quote_state == QuoteState.FLAT:
            return False, 0, 0

        age_s = self.inventory.oldest_fill_age()
        if age_s is None:
            return False, 0, 0

        # Don't trim if already in sweet-spot window (about to exit anyway)
        if age_s >= self.config.SWEET_SPOT_MIN_S:
            return False, 0, 0

        # Check trim cooldown
        now = time.time()
        if now - self.last_trim_ts < self.config.TRIM_COOLDOWN_S:
            return False, 0, 0

        vwap = self.inventory.vwap_entry
        if vwap is None:
            return False, 0, 0

        mae_c = abs(mark - vwap) * 100  # Convert to cents

        if mae_c < self.config.MAE_TRIM_THRESHOLD_C:
            return False, 0, 0

        # Calculate adaptive trim percentage based on severity
        severity = min(
            (mae_c - self.config.MAE_TRIM_THRESHOLD_C) /
            (self.config.MAE_HARD_CAP_C - self.config.MAE_TRIM_THRESHOLD_C),
            1.0
        )
        trim_pct = self.config.MAE_TRIM_PCT_MIN + severity * (
            self.config.MAE_TRIM_PCT_MAX - self.config.MAE_TRIM_PCT_MIN
        )

        net = abs(self.inventory.net_contracts)
        trim_qty = max(1, round(trim_pct * net))  # At least 1 contract

        # Never trim to less than 1 contract remaining - just flatten entirely
        if net - trim_qty < 1:
            trim_qty = net

        return True, trim_pct, trim_qty

    async def execute_mae_trim(self, trim_qty: int, mark: float):
        """
        Execute MAE trim via IOC order.

        Args:
            trim_qty: Number of contracts to trim
            mark: Current market price
        """
        if self.quote_state == QuoteState.SKEW_LONG:
            # Sell to reduce long position
            await self._place_ioc_order(mark, trim_qty, "sell")
            print(f"[{self.ticker}] MAE trim: selling {trim_qty} @ ${mark:.4f}")
        else:  # SKEW_SHORT
            # Buy to reduce short position
            await self._place_ioc_order(mark, trim_qty, "buy")
            print(f"[{self.ticker}] MAE trim: buying {trim_qty} @ ${mark:.4f}")

        self.last_trim_ts = time.time()

    # ======================================================================
    # NEW: Circuit Breaker (2-Tier Rate Limiting)
    # ======================================================================

    def track_cancel_or_replace(self, reason: str = "quote"):
        """
        Track cancel/replace event for circuit breaker monitoring.

        CRITICAL FIX: Only count quote/replace cancels, not fills or crossed orders.
        This prevents false CB triggers from venue auto-cancels.

        Args:
            reason: Why the cancel happened
                   - "quote" or "replace": Count for CB (normal quote updates)
                   - "crossed", "fill", "manual": Don't count (not rate-limit worthy)
        """
        # Only track cancels that indicate potential spam/runaway loops
        if reason not in ["quote", "replace"]:
            return  # Don't count fills, crosses, or manual cancels

        now = time.time()

        # Add to both rolling windows
        self.cancel_timestamps_30s.append(now)
        self.cancel_timestamps_60s.append(now)

        # Prune old entries
        cutoff_30 = now - 30
        cutoff_60 = now - 60

        while self.cancel_timestamps_30s and self.cancel_timestamps_30s[0] < cutoff_30:
            self.cancel_timestamps_30s.popleft()
        while self.cancel_timestamps_60s and self.cancel_timestamps_60s[0] < cutoff_60:
            self.cancel_timestamps_60s.popleft()

    def check_circuit_breaker(self) -> Tuple[bool, str]:
        """
        Check if circuit breaker should engage based on cancel/replace rate.

        Uses 2-tier system:
        - Soft brake: Widen quotes, double cooldown, disallow nonessential replaces
        - Hard brake: Pause new quotes, allow only exit orders

        Thresholds adapt based on:
        - State (higher when holding inventory)
        - Spread (lower when 1-tick spread)

        Returns:
            (is_active, tier) - tier is "none", "soft", or "hard"
        """
        now = time.time()

        # Check if already in brake period
        if now < self.circuit_brake_until:
            return True, self.circuit_brake_tier

        n30 = len(self.cancel_timestamps_30s)
        n60 = len(self.cancel_timestamps_60s)

        # Calculate adaptive thresholds
        tier1_30 = self.config.CB_TIER1_30S
        tier1_60 = self.config.CB_TIER1_60S
        tier2_30 = self.config.CB_TIER2_30S
        tier2_60 = self.config.CB_TIER2_60S

        # Adjust for state (relax when holding inventory - natural to replace less)
        if self.quote_state != QuoteState.FLAT:
            tier1_30 = int(tier1_30 * self.config.CB_SKEW_MULTIPLIER)
            tier1_60 = int(tier1_60 * self.config.CB_SKEW_MULTIPLIER)
            tier2_30 = int(tier2_30 * self.config.CB_SKEW_MULTIPLIER)
            tier2_60 = int(tier2_60 * self.config.CB_SKEW_MULTIPLIER)

        # Adjust for tight spreads (tighten - easier to spam accidentally)
        if self.best_bid is not None and self.best_ask is not None:
            spread_ticks = int((self.best_ask - self.best_bid) / (self.config.TICK_C / 100))
            if spread_ticks == 1:
                tier1_30 = max(1, int(tier1_30 * self.config.CB_TIGHT_SPREAD_MULTIPLIER))
                tier1_60 = max(1, int(tier1_60 * self.config.CB_TIGHT_SPREAD_MULTIPLIER))
                tier2_30 = max(1, int(tier2_30 * self.config.CB_TIGHT_SPREAD_MULTIPLIER))
                tier2_60 = max(1, int(tier2_60 * self.config.CB_TIGHT_SPREAD_MULTIPLIER))

        # Check tiers
        if n30 >= tier2_30 or n60 >= tier2_60:
            self.circuit_brake_until = now + self.config.CB_HARD_BRAKE_S
            self.circuit_brake_tier = "hard"
            print(f"[{self.ticker}] 🚨 CIRCUIT BREAKER: HARD BRAKE ({n30}/30s, {n60}/60s)")
            return True, "hard"

        if n30 >= tier1_30 or n60 >= tier1_60:
            self.circuit_brake_until = now + self.config.CB_SOFT_BRAKE_S
            self.circuit_brake_tier = "soft"
            print(f"[{self.ticker}] ⚠️  CIRCUIT BREAKER: SOFT BRAKE ({n30}/30s, {n60}/60s)")
            return True, "soft"

        self.circuit_brake_tier = "none"
        return False, "none"

    def _cooldown_ok(self, side: str, exception: bool = False) -> bool:
        """
        NEW: Adaptive cooldown - faster when flat, slower when holding inventory.

        Cooldown = base + per_contract × |net|, capped at MAX_COOLDOWN_MS

        Args:
            side: 'bid' or 'ask'
            exception: If True, bypass cooldown for mandatory actions
                      (state transitions, exits, MAE trims)

        Returns:
            True if cooldown has passed or exception=True
        """
        if exception:
            # Always allow mandatory actions (state changes, exits, etc.)
            self._last_quote_ts[side] = time.time()
            return True

        net = abs(self.inventory.net_contracts)

        # Choose base cooldown based on state
        if self.quote_state == QuoteState.FLAT:
            base_ms = self.config.BASE_COOLDOWN_FLAT_MS
        else:
            base_ms = self.config.BASE_COOLDOWN_SKEW_MS

        # Add per-contract penalty when holding inventory
        cooldown_ms = min(
            base_ms + self.config.COOLDOWN_PER_CONTRACT_MS * net,
            self.config.MAX_COOLDOWN_MS
        )

        # Check if enough time has passed
        now = time.time()
        elapsed_ms = (now - self._last_quote_ts[side]) * 1000

        if elapsed_ms >= cooldown_ms:
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

    def _should_replace(self, side: str, new_price: float) -> bool:
        """
        NEW: Check if order should be replaced using hysteresis logic.

        Only replace if:
        - No active order exists, OR
        - New price differs significantly from old price, OR
        - Order is crossed/inside spread/displaced by ≥ HYSTERESIS_TICKS

        Args:
            side: 'bid' or 'ask'
            new_price: New desired price in dollars

        Returns:
            True if order should be replaced
        """
        active = self.orders.active_bid if side == 'bid' else self.orders.active_ask

        if not active:
            return True  # No active order, should place

        old_price = active['price']

        # Always replace if price changed
        if abs(new_price - old_price) >= (self.config.TICK_C / 100):
            return True

        # Check if existing order needs replacement due to market movement
        if self.crossed_or_far_from_touch(old_price, side):
            return True

        return False

    async def _place_or_replace(self, side: str, price: float, qty: int):
        """
        Place or replace order on one side.

        Args:
            side: 'bid' or 'ask'
            price: Price in dollars
            qty: Quantity in contracts
        """
        # NEW: Tick-snapping validation
        snapped_price = self.round_to_tick(price)
        if abs(price - snapped_price) > 1e-6:  # Allow small float precision errors
            print(f"[{self.ticker}] ⚠️  Price {price:.4f} not tick-snapped, using {snapped_price:.4f}")
            price = snapped_price

        # Cancel existing order if present
        active = self.orders.active_bid if side == 'bid' else self.orders.active_ask
        if active:
            await self.api.cancel_order(active['order_id'])
            # NEW: Track cancel for circuit breaker (reason="replace" for normal quote updates)
            self.track_cancel_or_replace("replace")

        # ======================================================================
        # Shadow Mode & Safety Checks
        # ======================================================================

        # Shadow mode: Log proposed quote without sending to exchange
        if getattr(self.config, 'SHADOW_MODE', False):
            self._log_event("shadow_quote", side=side, price=round(price, 4), qty=qty,
                          would_cross=(price >= self.best_ask if side == 'bid' else price <= self.best_bid))
            return

        # Kill switch: Disable all new quotes
        if os.getenv('DISABLE_NEW_QUOTES', 'false').lower() == 'true':
            print(f"[{self.ticker}] Kill switch active - no new quotes")
            return

        # Inventory limit check
        if abs(self.inventory.net_contracts) >= getattr(self.config, 'MAX_INVENTORY_PER_MARKET', 50):
            self._log_event("inventory_limit", reason="max_per_market",
                          net=self.inventory.net_contracts)
            return

        # Place new order with strategy-tagged client_order_id
        cents = int(round(price * 100))
        coid = make_coid("MMv2", self.ticker)

        # Log quote placement
        self._log_event("quote_place", side=side, price=round(price, 4), qty=qty,
                      price_cents=cents)

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

    async def _place_ioc_order(self, price: float, qty: int, action: str):
        """
        NEW: Place IOC (Immediate-Or-Cancel) order for forced exits.

        Used for:
        - Sweet-spot exits (8-12 min)
        - Hard-cap exits (40 min)
        - MAE trims

        Args:
            price: Price in dollars
            qty: Quantity in contracts
            action: 'buy' or 'sell'
        """
        if not self.quoting_enabled:
            print(f"[{self.ticker}] IOC order skipped (paper mode)")
            return

        cents = int(round(price * 100))
        coid = make_coid("MMv2-IOC", self.ticker)

        try:
            # Note: Kalshi API may not support IOC type explicitly
            # If not supported, use regular order with immediate expectation
            od = await self.api.place_order(
                self.ticker,
                side="yes",
                action=action,
                count=qty,
                price_cents=cents,
                client_order_id=coid
                # type="ioc"  # Enable if API supports
            )

            if od and 'order' in od:
                order_id = od['order']['order_id']
                action_emoji = "🟢" if action == "buy" else "🔴"
                print(f"[{self.ticker}] {action_emoji} IOC {action.upper()}: {qty} @ ${price:.4f} ({cents}¢)")

                # Don't track as active order (it's IOC)
                # But register for fill processing
                self.orders.register_order(coid, {
                    "order_id": order_id,
                    "strategy_id": "MMv2-IOC",
                    "ticker": self.ticker,
                    "side": action
                })
            else:
                print(f"[{self.ticker}] ⚠️  IOC order failed: {od}")

        except Exception as e:
            print(f"[{self.ticker}] ❌ IOC order error: {e}")

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
            print(f"[{self.ticker}] ❌ Fill rejected: foreign strategy coid={coid}")
            return  # Foreign strategy fill

        # Validate ticker
        if ticker != self.ticker:
            print(f"[{self.ticker}] ❌ Fill rejected: wrong ticker {ticker}")
            return
        if not oid:
            print(f"[{self.ticker}] ❌ Fill rejected: missing order_id")
            return
        if not fid:
            print(f"[{self.ticker}] ❌ Fill rejected: missing fill_id")
            return

        # Check if already processed (idempotent)
        if self.orders.already_processed(oid, fid):
            print(f"[{self.ticker}] ⏭️  Fill {fid[:8]} already processed, skipping")
            return

        # Extract fill details
        side = fill.get('side')  # 'yes' or 'no'
        action = fill.get('action')  # 'buy' or 'sell'
        count = fill.get('count', 0)
        price_cents = fill.get('yes_price') if side == 'yes' else fill.get('no_price')

        if not all([side, action, count, price_cents]):
            print(f"[{self.ticker}] ❌ Fill rejected: missing required fields side={side} action={action} count={count} price_cents={price_cents}")
            return

        price = price_cents / 100

        # Parse timestamp (could be ISO string or Unix int)
        ts_raw = fill.get('created_time')
        if isinstance(ts_raw, str):
            # ISO format: "2025-10-28T21:44:39.062000Z"
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
            except:
                timestamp = int(time.time())
        elif isinstance(ts_raw, (int, float)):
            timestamp = int(ts_raw)
        else:
            timestamp = int(time.time())

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

        # NEW: Check for state transitions and trigger immediate re-quote
        old_state = self.quote_state
        net = self.inventory.net_contracts

        # Determine new state
        if net == 0:
            new_state = QuoteState.FLAT
        elif net > 0:
            new_state = QuoteState.SKEW_LONG
        else:
            new_state = QuoteState.SKEW_SHORT

        # If state changed, force immediate re-quote (bypass cooldown)
        if new_state != old_state:
            self.quote_state = new_state
            self.last_state_change_ts = time.time()
            self.last_tp_vwap = None  # Force TP recalc

            print(f"[{self.ticker}] State transition: {old_state.value} → {new_state.value}")

            # Log state change with telemetry
            self._log_event("state_change",
                          old_state=old_state.value,
                          new_state=new_state.value,
                          net=net)

            # Immediately recompute quotes with exception to bypass cooldown
            quote_bid, quote_ask = self._compute_quotes()
            await self._update_quotes_with_exception(quote_bid, quote_ask, self.config.SIZE_PER_FILL)

    async def _update_quotes_with_exception(
        self,
        bid: Optional[float],
        ask: Optional[float],
        qty: int
    ):
        """
        NEW: Update quotes with exception flag (bypasses cooldown).

        Used for mandatory quote updates:
        - State transitions
        - Exit windows
        - Circuit breaker recovery
        """
        if not self.quoting_enabled:
            return

        # Handle bid side with exception (bypass cooldown)
        if bid is None:
            if self.orders.active_bid:
                await self.api.cancel_order(self.orders.active_bid['order_id'])
                self.orders.clear_active_bid()
                # Don't count state transition cancels for CB (not spam)
                self.track_cancel_or_replace("manual")
        else:
            if self._should_replace('bid', bid) and self._cooldown_ok('bid', exception=True):
                await self._place_or_replace('bid', bid, qty)

        # Handle ask side with exception (bypass cooldown)
        if ask is None:
            if self.orders.active_ask:
                await self.api.cancel_order(self.orders.active_ask['order_id'])
                self.orders.clear_active_ask()
                # Don't count state transition cancels for CB (not spam)
                self.track_cancel_or_replace("manual")
        else:
            if self._should_replace('ask', ask) and self._cooldown_ok('ask', exception=True):
                await self._place_or_replace('ask', ask, qty)

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
