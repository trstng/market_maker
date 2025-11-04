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
    """State machine for market making strategy (matches backtest exactly)"""
    FLAT = "FLAT"  # Neutral zone (abs(net) < NEUTRAL_ZONE_THRESHOLD), quote both sides
    SKEW_LONG = "SKEW_LONG"  # Long inventory (net >= threshold), exit-only ASK
    SKEW_SHORT = "SKEW_SHORT"  # Short inventory (net <= -threshold), exit-only BID


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
        self.extreme_price_exit = False  # Set when mid hits extreme price (< 5¢ or > 95¢)

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

        # Pyramiding state tracking
        self.last_reentry_ts: float = 0.0
        self.last_entry_price: Optional[float] = None

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
        print(f"Circuit Breaker:")
        print(f"  Soft: {self.config.CB_TIER1_30S}/30s or {self.config.CB_TIER1_60S}/60s")
        print(f"  Hard: {self.config.CB_TIER2_30S}/30s or {self.config.CB_TIER2_60S}/60s")
        print(f"Pyramiding:")
        print(f"  Enabled: {self.config.ALLOW_PYRAMIDING}")
        print(f"  Max Contracts: {self.config.PYRAMID_MAX_CONTRACTS}")
        print(f"  Size Per Layer: {self.config.PYRAMID_SIZE_PER_FILL}")
        print(f"  Grid Step: {self.config.PYRAMID_STEP_C}¢")
        print(f"  Re-entry Cooldown: {self.config.PYRAMID_REENTRY_COOLDOWN_S}s")
        print(f"  Grid Anchor: {'VWAP' if self.config.PYRAMID_USE_VWAP_GRID else 'Last Fill'}")
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

        # Structured logs removed for cleaner output
        # event = {
        #     "ts": time.time(),
        #     "market": self.ticker,
        #     "state": self.quote_state.value,
        #     "net": self.inventory.net_contracts,
        #     "vwap": round(vwap, 4) if vwap else None,
        #     "tp": round(tp, 4) if tp else None,
        #     "best_bid": round(self.best_bid, 4) if self.best_bid else None,
        #     "best_ask": round(self.best_ask, 4) if self.best_ask else None,
        #     "action": action,
        #     **kwargs
        # }
        # print(json.dumps(event))

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
        Process incoming market message with DWE=500 + MAE failsafe exit logic.

        Priority order (BACKTEST-EXACT):
        1. Risk policy flatten (DWE ≥ 500 OR MAE ≥ 8¢)
        2. Normal quoting
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
        # Extreme Price Exit Gates (< 5¢ or > 95¢)
        # ======================================================================
        if hasattr(self.config, 'EXIT_PRICE_LOW') and hasattr(self.config, 'EXIT_PRICE_HIGH'):
            # Check if we hit extreme prices
            if (self.mid_ema <= self.config.EXIT_PRICE_LOW or
                self.mid_ema >= self.config.EXIT_PRICE_HIGH):

                if not self.extreme_price_exit:
                    # First time hitting extreme price - flatten if have position
                    if self.inventory.net_contracts != 0:
                        reason = f"EXTREME_PRICE_{self.mid_ema:.4f}"
                        print(f"[{self.ticker}] 🚨 EXTREME PRICE EXIT: Mid={self.mid_ema:.2f} | Flattening {self.inventory.net_contracts:+d} @ market")
                        await self._flatten(self.mid_ema, ts, reason)
                        await self._cancel_both()
                    # Set flag and stop quoting (even if flat)
                    self.extreme_price_exit = True
                    return
                else:
                    # Already flagged - check for unexpected position
                    if self.inventory.net_contracts != 0:
                        reason = f"EXTREME_PRICE_REENTRY_{self.mid_ema:.4f}"
                        print(f"[{self.ticker}] 🚨 RE-FLATTEN: Mid={self.mid_ema:.2f} | Flattening {self.inventory.net_contracts:+d} @ market")
                        await self._flatten(self.mid_ema, ts, reason)
                        await self._cancel_both()
                    return  # Stay out

            # Check if price has normalized - resume quoting
            elif self.extreme_price_exit:
                if (self.mid_ema >= self.config.EXIT_RESUME_LOW and
                    self.mid_ema <= self.config.EXIT_RESUME_HIGH):
                    print(f"[{self.ticker}] ✅ Price normalized ({self.mid_ema:.2f}) - resuming quoting")
                    self.extreme_price_exit = False
                    # Continue to normal quoting
                else:
                    return  # Still in 5-10¢ or 90-95¢ dead zone, stay out

        # ======================================================================
        # ONLY Exit Logic: DWE=500 + MAE Failsafe (matches backtest)
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

        # Update orders on exchange with dynamic sizing
        size = (self.config.SIZE_PER_FILL if self.quote_state == QuoteState.FLAT
                else self.config.PYRAMID_SIZE_PER_FILL)
        await self._update_quotes(quote_bid, quote_ask, size)

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

        # Determine state from inventory (BACKTEST LOGIC: neutral zone threshold)
        if abs(net) < self.config.NEUTRAL_ZONE_THRESHOLD:
            current_state = QuoteState.FLAT  # Quote both sides when abs(net) < 20 (backtest behavior)
        elif net > 0:
            current_state = QuoteState.SKEW_LONG  # Exit-only when net >= 20
        else:
            current_state = QuoteState.SKEW_SHORT  # Exit-only when net <= -20

        # Update state if changed
        if current_state != self.quote_state:
            self.quote_state = current_state
            self.last_state_change_ts = time.time()

        # ======================================================================
        # FLAT Mode: Quote both sides (BACKTEST LOGIC - no price gates)
        # ======================================================================
        if self.quote_state == QuoteState.FLAT:
            base_spread = self.config.BASE_SPREAD

            # Backtest logic: Always quote both sides when flat
            # quote_bid = mid_ema - base_spread
            # quote_ask = mid_ema + base_spread
            bid = self.round_to_tick(mid - base_spread)
            ask = self.round_to_tick(mid + base_spread)

            # Maker-only protection: never cross the spread
            if self.best_ask is not None and bid >= self.best_ask:
                bid = self.round_to_tick(self.best_ask - self.config.TICK_C / 100)
            if self.best_bid is not None and ask <= self.best_bid:
                ask = self.round_to_tick(self.best_bid + self.config.TICK_C / 100)

            return (bid, ask)

        # ======================================================================
        # SKEW_LONG Mode: ASK (passive exit) + optional BID (pyramiding re-entry)
        # ======================================================================
        elif self.quote_state == QuoteState.SKEW_LONG:
            mid = self.mid_ema
            if mid is None:
                return (None, None)

            # Calculate inventory skew factor (matches backtest logic)
            skew_factor = abs(self.inventory.net_contracts) / self.config.PYRAMID_MAX_CONTRACTS

            # Quote ask with widened spread (passive exit as market trades against us)
            # Backtest: quote_ask = mid + base_spread * (1 + skew)
            base_spread = self.config.BASE_SPREAD
            ask = self.round_to_tick(mid + base_spread * (1 + skew_factor))

            # Ensure ask never inside touch
            if self.best_bid is not None and ask <= self.best_bid:
                ask = self.round_to_tick(self.best_bid + self.config.TICK_C / 100)

            # NO PYRAMIDING - backtest only quotes exit side when holding inventory
            bid = None

            return (bid, ask)

        # ======================================================================
        # SKEW_SHORT Mode: BID (passive exit) + optional ASK (pyramiding re-entry)
        # ======================================================================
        elif self.quote_state == QuoteState.SKEW_SHORT:
            mid = self.mid_ema
            if mid is None:
                return (None, None)

            # Calculate inventory skew factor (matches backtest logic)
            skew_factor = abs(self.inventory.net_contracts) / self.config.PYRAMID_MAX_CONTRACTS

            # Quote bid with widened spread (passive exit as market trades against us)
            # Backtest: quote_bid = mid - base_spread * (1 + skew)
            base_spread = self.config.BASE_SPREAD
            bid = self.round_to_tick(mid - base_spread * (1 + skew_factor))

            # Ensure bid never inside touch
            if self.best_ask is not None and bid >= self.best_ask:
                bid = self.round_to_tick(self.best_ask - self.config.TICK_C / 100)

            # NO PYRAMIDING - backtest only quotes exit side when holding inventory
            ask = None

            return (bid, ask)

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
    # Circuit Breaker (2-Tier Rate Limiting)
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
            # Circuit breaker log removed for cleaner output
            return True, "hard"

        if n30 >= tier1_30 or n60 >= tier1_60:
            self.circuit_brake_until = now + self.config.CB_SOFT_BRAKE_S
            self.circuit_brake_tier = "soft"
            # Circuit breaker log removed for cleaner output
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
        Check if order should be replaced using hysteresis logic (BACKTEST-MATCHING).

        Only replace if:
        - No active order exists, OR
        - Order is crossed/inside spread, OR
        - In FLAT mode: moved by ≥ HYSTERESIS_TICKS
        - In SKEW mode: never replace for displacement (TP is VWAP-anchored)

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

        # Always check if crossed/inside spread
        if self.crossed_or_far_from_touch(old_price, side):
            return True

        # Calculate price movement in ticks
        tick = self.config.TICK_C / 100
        delta_ticks = abs(new_price - old_price) / tick

        # FLAT mode: only replace if moved by HYSTERESIS_TICKS (stops constant requoting)
        if self.quote_state == QuoteState.FLAT:
            return delta_ticks >= self.config.HYSTERESIS_TICKS

        # SKEW mode: never replace for displacement (TP stays VWAP-anchored)
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
            # Price adjustment log removed for cleaner output
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
            # Kill switch log removed for cleaner output
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

    async def _flatten(self, exit_px: float, ts: int, reason: str):
        """
        Flatten entire inventory (risk policy trigger).

        Args:
            exit_px: Exit price
            ts: Timestamp
            reason: Trigger reason
        """
        # Cancel all active orders first
        await self._cancel_both()

        # Place order to exit position on Kalshi at best available price
        net = self.inventory.net_contracts
        if net != 0:
            if net > 0:
                # Exit long: SELL at best bid (take liquidity, immediate fill)
                if self.best_bid is not None:
                    exit_price_cents = int(self.best_bid * 100)
                elif self.mid_ema is not None:
                    # Fallback: cross mid by 1 tick
                    exit_price_cents = max(1, int(self.mid_ema * 100) - 1)
                else:
                    # Emergency fallback
                    exit_price_cents = 50

                print(f"[{self.ticker}] 🚨 FLATTEN: SELL {abs(net)} @ {exit_price_cents}¢ (best_bid)")

                await self.api.place_order(
                    self.ticker,
                    side="yes",
                    action="sell",
                    count=abs(net),
                    price_cents=exit_price_cents
                )
            else:
                # Exit short: BUY at best ask (take liquidity, immediate fill)
                if self.best_ask is not None:
                    exit_price_cents = int(self.best_ask * 100)
                elif self.mid_ema is not None:
                    # Fallback: cross mid by 1 tick
                    exit_price_cents = min(99, int(self.mid_ema * 100) + 1)
                else:
                    # Emergency fallback
                    exit_price_cents = 50

                print(f"[{self.ticker}] 🚨 FLATTEN: BUY {abs(net)} @ {exit_price_cents}¢ (best_ask)")

                await self.api.place_order(
                    self.ticker,
                    side="yes",
                    action="buy",
                    count=abs(net),
                    price_cents=exit_price_cents
                )

        # Update internal state
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
            # Fill rejection log removed for cleaner output
            return  # Foreign strategy fill

        # Validate ticker
        if ticker != self.ticker:
            # Fill rejection log removed for cleaner output
            return
        if not oid:
            # Fill rejection log removed for cleaner output
            return
        if not fid:
            # Fill rejection log removed for cleaner output
            return

        # Check if already processed (idempotent)
        if self.orders.already_processed(oid, fid):
            # Duplicate fill log removed for cleaner output
            return

        # Extract fill details
        side = fill.get('side')  # 'yes' or 'no'
        action = fill.get('action')  # 'buy' or 'sell'
        count = fill.get('count', 0)
        price_cents = fill.get('yes_price') if side == 'yes' else fill.get('no_price')

        if not all([side, action, count, price_cents]):
            # Fill rejection log removed for cleaner output
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
            # BUY YES: check if closing short or opening long
            if self.inventory.net_contracts < 0:
                # Have short position to close
                fees = fees_roundtrip(count, abs(self.inventory.vwap_entry), price)
                self._realize_position(count, price, timestamp, fees, 'fill_from_exchange')
            else:
                # Opening long position
                self._execute_fill(count, price, timestamp, 'long')
                # Track for pyramiding re-entry cooldown
                self.last_entry_price = price
                self.last_reentry_ts = time.time()
        elif action == 'sell' and side == 'yes':
            # SELL YES: check if closing long or opening short
            if self.inventory.net_contracts > 0:
                # Have YES position to sell - closing long
                fees = fees_roundtrip(count, self.inventory.vwap_entry, price)
                self._realize_position(count, price, timestamp, fees, 'fill_from_exchange')
            else:
                # Don't have YES position - selling YES means opening short (equivalent to buying NO)
                self._execute_fill(count, price, timestamp, 'short')
                self.last_entry_price = price
                self.last_reentry_ts = time.time()
        elif action == 'buy' and side == 'no':
            # BUY NO: check if closing short, closing long, or opening short
            yes_price = 1.0 - price
            if self.inventory.net_contracts < 0:
                # Have SHORT - BUY NO closes short (equivalent to BUY YES)
                fees = fees_roundtrip(count, abs(self.inventory.vwap_entry), yes_price)
                self._realize_position(count, yes_price, timestamp, fees, 'fill_from_exchange')
            elif self.inventory.net_contracts > 0:
                # Have LONG YES - BUY NO closes it (offsets YES position)
                fees = fees_roundtrip(count, self.inventory.vwap_entry, yes_price)
                self._realize_position(count, yes_price, timestamp, fees, 'fill_from_exchange')
            else:
                # FLAT - BUY NO opens SHORT (equivalent to SELL YES)
                self._execute_fill(count, yes_price, timestamp, 'short')
                # Track for pyramiding re-entry cooldown
                self.last_entry_price = yes_price
                self.last_reentry_ts = time.time()
        elif action == 'sell' and side == 'no':
            # SELL NO: Convert NO price to YES price for consistent tracking
            yes_price = 1.0 - price
            if self.inventory.net_contracts > 0:
                # Have LONG YES - SELL NO closes it (offsets YES position)
                fees = fees_roundtrip(count, self.inventory.vwap_entry, yes_price)
                self._realize_position(count, yes_price, timestamp, fees, 'fill_from_exchange')
            elif self.inventory.net_contracts < 0:
                # Have SHORT - SELL NO adds to short (selling more NO you don't have)
                self._execute_fill(count, yes_price, timestamp, 'short')
                self.last_entry_price = yes_price
                self.last_reentry_ts = time.time()
            else:
                # FLAT - SELL NO opens SHORT (not long! you're selling NO you don't have)
                self._execute_fill(count, yes_price, timestamp, 'short')
                self.last_entry_price = yes_price
                self.last_reentry_ts = time.time()

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

        # Determine new state (MUST match _compute_quotes logic!)
        if abs(net) < self.config.NEUTRAL_ZONE_THRESHOLD:
            new_state = QuoteState.FLAT
        elif net > 0:
            new_state = QuoteState.SKEW_LONG
        else:
            new_state = QuoteState.SKEW_SHORT

        # If state changed, force immediate re-quote (bypass cooldown)
        if new_state != old_state:
            self.quote_state = new_state
            self.last_state_change_ts = time.time()

            # State transition log removed for cleaner output

            # Log state change with telemetry
            self._log_event("state_change",
                          old_state=old_state.value,
                          new_state=new_state.value,
                          net=net)

            # Immediately recompute quotes with exception to bypass cooldown
            quote_bid, quote_ask = self._compute_quotes()
            size = (self.config.SIZE_PER_FILL if self.quote_state == QuoteState.FLAT
                    else self.config.PYRAMID_SIZE_PER_FILL)
            await self._update_quotes_with_exception(quote_bid, quote_ask, size)

        # CRITICAL FIX: Mark fill as processed AFTER successful processing
        # This prevents fills from being marked as processed if they fail validation or processing
        self.orders.mark_processed(oid, fid)

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
