"""
Backtest: Inventory Management MM - PROPER IMPLEMENTATION (NHL)

Maintains single inventory with layered fills, VWAP-anchored MAE tracking,
and inventory-level risk controls (not per-trade stops).

Risk Controls Tested:
1. Baseline: No inventory controls
2. MAE-of-Inventory: Exit when abs(mark - vwap) >= X cents
3. Time-at-Risk: Exit when duration_weighted_exposure exceeds threshold
4. Dynamic Time Governor: Exit when age > k × median(recent_realizations)
"""
import sys
from pathlib import Path
import csv
from datetime import datetime
from collections import deque
import statistics

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from supabase import create_client
from config.settings import settings

supabase = create_client(settings.supabase_url, settings.supabase_key)


def kalshi_fee(contracts, price_dollars):
    """Calculate Kalshi fee for given contracts and price (in dollars)."""
    import math
    fee = 0.0175 * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(fee * 100) / 100  # Round up to nearest cent


def fees_roundtrip(contracts, entry_price, exit_price):
    """Calculate total fees for entry + exit."""
    return kalshi_fee(contracts, entry_price) + kalshi_fee(contracts, exit_price)


class Fill:
    """Represents a single fill (layer in inventory)."""
    def __init__(self, qty, price, timestamp, side):
        self.qty = qty  # Contracts
        self.price = price  # Entry price
        self.timestamp = timestamp
        self.side = side  # 'long' or 'short'

    def age_seconds(self, current_time):
        return current_time - self.timestamp


class InventoryState:
    """Maintains inventory with layered fills and VWAP tracking."""

    def __init__(self):
        self.layers = deque()  # [(Fill), ...]
        self.net_contracts = 0  # Signed: positive=long, negative=short
        self.realized_pnl = 0.0
        self.total_fills = 0

        # Track realizations for dynamic time control
        self.recent_realizations = deque(maxlen=50)  # Last 50 micro-round-trips

    @property
    def vwap_entry(self):
        """Volume-weighted average price of current inventory."""
        if not self.layers:
            return 0.0
        total_notional = sum(f.qty * f.price for f in self.layers)
        total_qty = sum(f.qty for f in self.layers)
        return total_notional / total_qty if total_qty > 0 else 0.0

    def inventory_mae(self, current_price):
        """MAE of inventory (VWAP-anchored)."""
        if not self.layers:
            return 0.0
        vwap = self.vwap_entry
        if self.net_contracts > 0:  # Long
            return max(0, vwap - current_price)
        else:  # Short
            return max(0, current_price - vwap)

    def oldest_fill_age(self, current_time):
        """Age of oldest fill in seconds."""
        if not self.layers:
            return 0
        return min(f.age_seconds(current_time) for f in self.layers)

    def duration_weighted_exposure(self, current_time):
        """Sum of |qty| × age_minutes for all layers."""
        return sum(abs(f.qty) * (f.age_seconds(current_time) / 60) for f in self.layers)

    def add_fill(self, qty, price, timestamp, side):
        """Add new fill to inventory."""
        self.layers.append(Fill(qty, price, timestamp, side))
        if side == 'long':
            self.net_contracts += qty
        else:
            self.net_contracts -= qty
        self.total_fills += 1

    def realize_pnl(self, qty, exit_price, exit_time, fees=0.0):
        """Realize P&L on offsetting fills (FIFO)."""
        qty_to_realize = qty
        hold_times = []
        realizations = []  # Track each realization event

        while qty_to_realize > 0 and self.layers:
            layer = self.layers[0]
            realized_qty = min(layer.qty, qty_to_realize)

            # Calculate P&L
            if layer.side == 'long':
                pnl = realized_qty * (exit_price - layer.price) - fees
            else:
                pnl = realized_qty * (layer.price - exit_price) - fees

            self.realized_pnl += pnl

            # Track hold time for this realization
            hold_time = exit_time - layer.timestamp
            hold_times.append(hold_time)

            # Record realization event
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

        return realizations

    def flatten_all(self, exit_price, exit_time, fees=0.0):
        """Close entire inventory at current price."""
        all_realizations = []
        while self.layers:
            layer = self.layers[0]
            realizations = self.realize_pnl(layer.qty, exit_price, exit_time, fees)
            all_realizations.extend(realizations)
        return all_realizations

    def get_median_realization_hold(self):
        """Get median hold time of recent realizations."""
        if len(self.recent_realizations) < 5:
            return 180  # Default 3 minutes
        return statistics.median(self.recent_realizations)


class InventoryRiskPolicy:
    """Defines risk control policies for inventory management."""

    def __init__(self, policy_type='baseline', **params):
        self.policy_type = policy_type
        self.params = params

        # Extract parameters
        self.mae_limit_cents = params.get('mae_limit_cents', 3.0)  # 3 cent MAE limit
        self.time_limit_minutes = params.get('time_limit_minutes', 30)  # 30 min time limit
        self.dynamic_multiplier = params.get('dynamic_multiplier', 5.0)  # 5× median
        self.exposure_limit = params.get('exposure_limit', 500)  # Duration-weighted limit

    def should_flatten(self, inventory, current_price, current_time):
        """Check if inventory should be flattened based on policy."""
        if self.policy_type == 'baseline':
            return False, None

        elif self.policy_type == 'mae_inventory':
            mae = inventory.inventory_mae(current_price)
            if mae >= self.mae_limit_cents / 100:  # Convert cents to dollars
                return True, f'mae_limit_{mae:.2f}c'

        elif self.policy_type == 'time_at_risk':
            age = inventory.oldest_fill_age(current_time) / 60  # Minutes
            if age >= self.time_limit_minutes:
                return True, f'time_limit_{age:.0f}min'

        elif self.policy_type == 'dynamic_time':
            median_hold = inventory.get_median_realization_hold()
            max_hold = self.dynamic_multiplier * median_hold
            max_hold = max(60, min(1800, max_hold))  # Clamp 60s-30min

            age = inventory.oldest_fill_age(current_time)
            if age >= max_hold:
                return True, f'dynamic_time_{age:.0f}s_limit_{max_hold:.0f}s'

        elif self.policy_type == 'duration_weighted':
            dwe = inventory.duration_weighted_exposure(current_time)
            if dwe >= self.exposure_limit:
                return True, f'duration_weighted_{dwe:.0f}'

        return False, None

    def get_inventory_skew(self, net_contracts, size_cap=100):
        """Calculate quote skew based on inventory position."""
        if abs(net_contracts) < 20:
            return 0.0  # Neutral

        # Sigmoid-like skew: more extreme as we approach limits
        bias = net_contracts / size_cap
        return bias  # Positive = long (widen asks), negative = short (widen bids)


def backtest_with_policy(policy_type='baseline', **policy_params):
    """Run inventory MM backtest with specified risk policy."""

    print(f"\n{'='*80}")
    print(f"INVENTORY MM BACKTEST (NHL) - {policy_type.upper().replace('_', ' ')}")
    if policy_params:
        print(f"Parameters: {policy_params}")
    print(f"{'='*80}\n")

    # Get settled NHL games
    response = supabase.table('market_metadata').select('*').eq('series_ticker', 'NHL').execute()
    settled = [m for m in response.data if m.get('result')]
    print(f"Found {len(settled)} settled NHL games\n")

    total_realized_pnl = 0.0
    all_game_results = []
    policy_triggers = {'total': 0}

    # CSV logging
    fill_log = []
    realization_log = []
    trigger_log = []

    for i, game in enumerate(settled, 1):
        ticker = game['market_ticker']
        title = game.get('title', ticker)
        result = game.get('result')

        print(f"[{i}/{len(settled)}] {title[:60]}")

        # Get trades
        trades = []
        batch_size = 1000
        offset = 0
        while True:
            batch = supabase.table('trades').select('*').eq('market_ticker', ticker)\
                .order('timestamp').range(offset, offset + batch_size - 1).execute()
            if not batch.data:
                break
            trades.extend(batch.data)
            if len(batch.data) < batch_size:
                break
            offset += batch_size

        if len(trades) < 100:
            print(f"  Insufficient data")
            continue

        # Initialize for this game
        inventory = InventoryState()
        policy = InventoryRiskPolicy(policy_type, **policy_params)

        game_fills = 0
        game_realizations = 0
        game_policy_triggers = 0

        # Track mid price with simple EMA
        mid_ema = None
        ema_alpha = 0.3

        for idx, trade in enumerate(trades):
            timestamp = trade['timestamp']
            price = trade.get('price')
            taker_side = trade.get('taker_side')

            if not price or not taker_side:
                continue

            price_dollars = price / 100

            # Update mid price EMA
            if mid_ema is None:
                mid_ema = price_dollars
            else:
                mid_ema = ema_alpha * price_dollars + (1 - ema_alpha) * mid_ema

            # Check if we should flatten inventory
            should_flatten, reason = policy.should_flatten(inventory, mid_ema, timestamp)
            if should_flatten:
                # Log trigger event with P&L before flatten
                pnl_before = inventory.realized_pnl
                trigger_log.append({
                    'game': title,
                    'ticker': ticker,
                    'timestamp': timestamp,
                    'trigger_reason': reason,
                    'policy_type': policy_type,
                    'net_contracts_before': inventory.net_contracts,
                    'vwap_entry': inventory.vwap_entry,
                    'inventory_mae': inventory.inventory_mae(mid_ema),
                    'oldest_fill_age_seconds': inventory.oldest_fill_age(timestamp),
                    'exit_price': mid_ema,
                    'pnl_before_flatten': pnl_before
                })

                realizations = inventory.flatten_all(mid_ema, timestamp, fees=0.0)

                # Log realizations from flatten
                pnl_from_flatten = 0
                for r in realizations:
                    pnl_from_flatten += r['pnl']
                    realization_log.append({
                        'game': title,
                        'ticker': ticker,
                        'timestamp': r['exit_time'],
                        'realized_qty': r['realized_qty'],
                        'entry_price': r['entry_price'],
                        'exit_price': r['exit_price'],
                        'entry_time': r['entry_time'],
                        'hold_time_seconds': r['hold_time_seconds'],
                        'hold_time_minutes': r['hold_time_seconds'] / 60,
                        'side': r['side'],
                        'pnl': r['pnl'],
                        'exit_reason': 'policy_trigger',
                        'remaining_net_contracts': inventory.net_contracts
                    })

                # Update last trigger with P&L from flatten
                if trigger_log:
                    trigger_log[-1]['pnl_from_flatten'] = pnl_from_flatten
                    trigger_log[-1]['pnl_after_flatten'] = inventory.realized_pnl

                game_policy_triggers += 1
                policy_triggers[reason] = policy_triggers.get(reason, 0) + 1
                policy_triggers['total'] += 1

            # Calculate inventory skew
            skew = policy.get_inventory_skew(inventory.net_contracts, size_cap=100)

            # Determine if we quote and get filled
            # Use mid EMA, quote at mid ± spread, adjust for skew
            base_spread = 0.01

            # Only quote if spread is reasonable (< 2 cents)
            # (In real backtest we'd check actual book spread)

            # Queue model: assume we're ~20% of top-of-book
            my_queue_share = 0.2
            trade_size = trade.get('count', 10)  # Fallback to 10 if not available

            # If neutral inventory (|net| < 20), quote both sides
            if abs(inventory.net_contracts) < 20:
                quote_bid = mid_ema - base_spread
                quote_ask = mid_ema + base_spread

                # CORRECTED: Seller ("no" taker) hits our bid if trade_price <= bid
                if taker_side == 'no' and price_dollars <= quote_bid:
                    # We provide bid liquidity (buy at our bid)
                    filled_qty = min(10, int(my_queue_share * trade_size))
                    if filled_qty > 0:
                        fill_price = quote_bid
                        entry_fee = kalshi_fee(filled_qty, fill_price)
                        inventory.add_fill(filled_qty, fill_price, timestamp, 'long')
                        game_fills += 1

                        # Log fill
                        fill_log.append({
                            'game': title,
                            'ticker': ticker,
                            'timestamp': timestamp,
                            'qty': filled_qty,
                            'price': fill_price,
                            'side': 'long',
                            'fee': entry_fee,
                            'net_contracts_after': inventory.net_contracts,
                            'vwap_after': inventory.vwap_entry
                        })

                # CORRECTED: Buyer ("yes" taker) lifts our ask if trade_price >= ask
                elif taker_side == 'yes' and price_dollars >= quote_ask:
                    # We provide ask liquidity (sell at our ask)
                    filled_qty = min(10, int(my_queue_share * trade_size))
                    if filled_qty > 0:
                        fill_price = quote_ask
                        entry_fee = kalshi_fee(filled_qty, fill_price)
                        inventory.add_fill(filled_qty, fill_price, timestamp, 'short')
                        game_fills += 1

                        # Log fill
                        fill_log.append({
                            'game': title,
                            'ticker': ticker,
                            'timestamp': timestamp,
                            'qty': filled_qty,
                            'price': fill_price,
                            'side': 'short',
                            'fee': entry_fee,
                            'net_contracts_after': inventory.net_contracts,
                            'vwap_after': inventory.vwap_entry
                        })

            # If long inventory (net > 20), prefer to sell (widen ask less)
            elif inventory.net_contracts > 20:
                quote_ask = mid_ema + base_spread * (1 + abs(skew))

                # CORRECTED: Buyer hits our ask if trade_price >= ask
                if taker_side == 'yes' and price_dollars >= quote_ask:
                    filled_qty = min(10, int(my_queue_share * trade_size))
                    if filled_qty > 0:
                        exit_price = quote_ask
                        # Calculate fees for roundtrip
                        roundtrip_fees = fees_roundtrip(filled_qty, inventory.vwap_entry, exit_price)
                        realizations = inventory.realize_pnl(filled_qty, exit_price, timestamp, fees=roundtrip_fees)
                        game_realizations += 1

                        # Log realizations
                        for r in realizations:
                            realization_log.append({
                                'game': title,
                                'ticker': ticker,
                                'timestamp': r['exit_time'],
                                'realized_qty': r['realized_qty'],
                                'entry_price': r['entry_price'],
                                'exit_price': r['exit_price'],
                                'entry_time': r['entry_time'],
                                'hold_time_seconds': r['hold_time_seconds'],
                                'hold_time_minutes': r['hold_time_seconds'] / 60,
                                'side': r['side'],
                                'pnl': r['pnl'],
                                'exit_reason': 'normal_realization',
                                'remaining_net_contracts': inventory.net_contracts
                            })

            # If short inventory (net < -20), prefer to buy (widen bid less)
            elif inventory.net_contracts < -20:
                quote_bid = mid_ema - base_spread * (1 + abs(skew))

                # CORRECTED: Seller hits our bid if trade_price <= bid
                if taker_side == 'no' and price_dollars <= quote_bid:
                    filled_qty = min(10, int(my_queue_share * trade_size))
                    if filled_qty > 0:
                        exit_price = quote_bid
                        # Calculate fees for roundtrip
                        roundtrip_fees = fees_roundtrip(filled_qty, abs(inventory.vwap_entry), exit_price)
                        realizations = inventory.realize_pnl(filled_qty, exit_price, timestamp, fees=roundtrip_fees)
                        game_realizations += 1

                        # Log realizations
                        for r in realizations:
                            realization_log.append({
                                'game': title,
                                'ticker': ticker,
                                'timestamp': r['exit_time'],
                                'realized_qty': r['realized_qty'],
                                'entry_price': r['entry_price'],
                                'exit_price': r['exit_price'],
                                'entry_time': r['entry_time'],
                                'hold_time_seconds': r['hold_time_seconds'],
                                'hold_time_minutes': r['hold_time_seconds'] / 60,
                                'side': r['side'],
                                'pnl': r['pnl'],
                                'exit_reason': 'normal_realization',
                                'remaining_net_contracts': inventory.net_contracts
                            })

        # Settle remaining inventory
        settlement_price = 1.0 if result == 'yes' else 0.0
        if trades:
            settlement_time = trades[-1]['timestamp']
            realizations = inventory.flatten_all(settlement_price, settlement_time, fees=0.0)

            # Log settlement realizations
            for r in realizations:
                realization_log.append({
                    'game': title,
                    'ticker': ticker,
                    'timestamp': r['exit_time'],
                    'realized_qty': r['realized_qty'],
                    'entry_price': r['entry_price'],
                    'exit_price': r['exit_price'],
                    'entry_time': r['entry_time'],
                    'hold_time_seconds': r['hold_time_seconds'],
                    'hold_time_minutes': r['hold_time_seconds'] / 60,
                    'side': r['side'],
                    'pnl': r['pnl'],
                    'exit_reason': 'settlement',
                    'remaining_net_contracts': inventory.net_contracts
                })

        game_pnl = inventory.realized_pnl
        total_realized_pnl += game_pnl

        all_game_results.append({
            'game': title,
            'pnl': game_pnl,
            'fills': game_fills,
            'realizations': game_realizations,
            'policy_triggers': game_policy_triggers
        })

        print(f"  Fills: {game_fills} | Realizations: {game_realizations} | P&L: ${game_pnl:+.2f} | Triggers: {game_policy_triggers}")

    print(f"\n{'='*80}")
    print(f"SUMMARY - {policy_type.upper().replace('_', ' ')}")
    print(f"{'='*80}")
    print(f"Total Games: {len(all_game_results)}")
    print(f"Total P&L: ${total_realized_pnl:,.2f}")
    print(f"Avg P&L per Game: ${total_realized_pnl/len(all_game_results):.2f}" if all_game_results else "")
    print(f"Policy Triggers: {policy_triggers['total']}")
    if policy_triggers['total'] > 0:
        print(f"  Breakdown:")
        for reason, count in policy_triggers.items():
            if reason != 'total':
                print(f"    {reason}: {count}")
    print()

    # Export to CSV
    csv_prefix = f"inventory_mm_{policy_type}"

    # Export fills
    if fill_log:
        fill_csv = f"backtest/nhl/{csv_prefix}_fills.csv"
        with open(fill_csv, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['game', 'ticker', 'timestamp', 'qty', 'price', 'side', 'fee', 'net_contracts_after', 'vwap_after'])
            writer.writeheader()
            writer.writerows(fill_log)
        print(f"Exported {len(fill_log)} fills to {fill_csv}")

    # Export realizations
    if realization_log:
        real_csv = f"backtest/nhl/{csv_prefix}_realizations.csv"
        with open(real_csv, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['game', 'ticker', 'timestamp', 'realized_qty', 'entry_price', 'exit_price', 'entry_time', 'hold_time_seconds', 'hold_time_minutes', 'side', 'pnl', 'exit_reason', 'remaining_net_contracts'])
            writer.writeheader()
            writer.writerows(realization_log)
        print(f"Exported {len(realization_log)} realizations to {real_csv}")

    # Export triggers
    if trigger_log:
        trigger_csv = f"backtest/nhl/{csv_prefix}_triggers.csv"
        with open(trigger_csv, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['game', 'ticker', 'timestamp', 'trigger_reason', 'policy_type', 'net_contracts_before', 'vwap_entry', 'inventory_mae', 'oldest_fill_age_seconds', 'exit_price', 'pnl_before_flatten', 'pnl_from_flatten', 'pnl_after_flatten'])
            writer.writeheader()
            writer.writerows(trigger_log)
        print(f"Exported {len(trigger_log)} triggers to {trigger_csv}")

    print()

    return {
        'policy': policy_type,
        'params': policy_params,
        'total_pnl': total_realized_pnl,
        'games': all_game_results,
        'policy_triggers': policy_triggers,
        'fill_count': len(fill_log),
        'realization_count': len(realization_log),
        'trigger_count': len(trigger_log)
    }


if __name__ == '__main__':
    results = []

    # Test 1: Baseline (no controls)
    results.append(backtest_with_policy('baseline'))

    # Test 2: MAE-of-Inventory (3 cent limit)
    results.append(backtest_with_policy('mae_inventory', mae_limit_cents=3.0))

    # Test 3: Time-at-Risk (30 min limit)
    results.append(backtest_with_policy('time_at_risk', time_limit_minutes=30))

    # Test 4: Dynamic Time Governor (5× median)
    results.append(backtest_with_policy('dynamic_time', dynamic_multiplier=5.0))

    # Test 5: Duration-Weighted Exposure
    results.append(backtest_with_policy('duration_weighted', exposure_limit=500))

    # Print comparison
    print(f"\n{'='*80}")
    print("POLICY COMPARISON (NHL)")
    print(f"{'='*80}\n")

    print(f"{'Policy':<30} {'Total P&L':<15} {'Triggers':<12} {'Avg P&L/Game':<15}")
    print("-" * 80)
    for r in results:
        avg_pnl = r['total_pnl'] / len(r['games']) if r['games'] else 0
        print(f"{r['policy']:<30} ${r['total_pnl']:>13,.2f} {r['policy_triggers']['total']:>11} ${avg_pnl:>13.2f}")

    print(f"\n{'='*80}\n")
