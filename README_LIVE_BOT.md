# Live Market Making Bot - NHL Edition

**Strategy:** Hybrid inventory management with duration-weighted primary + MAE failsafe
**Optimized for:** NHL goal-event volatility capture with tail risk protection
**Status:** Production-ready framework (requires API integration)

---

## Quick Start

### 1. Prerequisites

```bash
# Python 3.9+
pip install supabase-py
```

### 2. Configuration

Edit `live_mm_bot.py` → `BotConfig` class:

```python
class BotConfig:
    # Risk parameters
    DURATION_WEIGHTED_LIMIT = 500      # contracts × minutes
    MAE_FAILSAFE_CENTS = 8.0          # 8¢ MAE limit
    MAE_ACTIVATION_WINDOW_SEC = 8     # Must persist 8 seconds

    # Trading parameters
    BASE_SPREAD = 0.01                 # 1¢ spread
    SIZE_PER_FILL = 10                # Contracts per fill
    MAX_INVENTORY_VALUE = 100         # Max $100 per side
```

### 3. Run the Bot

```bash
python live_mm_bot.py
```

---

## Strategy Overview

### Primary Governor: Duration-Weighted Exposure

**What it does:**
Exits when `sum(contracts × age_minutes)` exceeds 500

**Why it works for NHL:**
- ✅ Survives goal-event volatility spikes (8-12¢ moves)
- ✅ Gives positions room to mean-revert (median hold: 10 minutes)
- ✅ Spread-agnostic (doesn't panic on bid/ask widening)
- ✅ Proven: **$406 total P&L** across 30 games (66.7% win rate)

**Example:**
```
Hold 10 contracts for 50 minutes = 500 exposure → Exit
Hold 50 contracts for 10 minutes = 500 exposure → Exit
```

---

### Failsafe: MAE 8¢ Circuit Breaker

**What it does:**
Exits if price moves 8¢ against VWAP entry **AND** persists >8 seconds

**Why the persistence check:**
- ❌ **Without:** Triggers on noise during goal-event chaos
- ✅ **With 8s window:** Only catches sustained momentum breaks

**Example:**
```
Goal scored → Price drops 12¢ → Recovers to -4¢ in 6s
→ MAE never persisted 8s → No trigger (correct!)

Momentum break → Price drops 15¢ → Stays there for 12s
→ MAE persisted 8s → Trigger! (tail risk protection)
```

**Data:**
- Only 19.1% of original MAE triggers were >10¢
- 75th percentile: 8.25¢
- This failsafe catches **extreme** moves, not normal volatility

---

## Backtest Performance

| Metric | Duration-Weighted | MAE 3¢ (too tight) | Hybrid (Expected) |
|--------|-------------------|---------------------|-------------------|
| Total P&L | $406.01 | $650.83 | $500-550 |
| Win Rate | 66.7% | 76.7% | 70-75% |
| Std Dev | $21.55 | $23.63 | $20-22 |
| Sharpe | 0.628 | 0.918 | 0.70-0.80 |
| Triggers/Game | 8 | 55 | 12-15 |
| Median Hold | 10.3 min | 27s | 8-12 min |
| Exit Quality | +$0.0447 | +$0.0371 | +$0.042 |

**Why Hybrid for Live Trading:**
- Duration captures goal-event mean reversions (primary edge)
- MAE 8¢ protects against catastrophic tail events
- Expected: 70-80% of MAE P&L with duration's smoothness

---

## How the Bot Works

### Trade Processing Flow

```
1. New trade arrives (from WebSocket/REST)
   ↓
2. Update mid price EMA
   ↓
3. Check if policy says flatten:
   a) Duration-weighted > 500? → Flatten
   b) MAE > 8¢ for >8s? → Flatten
   ↓
4. If not flattening, calculate inventory skew
   ↓
5. Quote logic:
   - Neutral (|net| < 20): Quote both bid/ask
   - Long (net > 20): Quote ask only (prefer to sell)
   - Short (net < -20): Quote bid only (prefer to buy)
   ↓
6. Execute fills based on taker side:
   - "no" taker hits our bid → We buy (long)
   - "yes" taker hits our ask → We sell (short)
   ↓
7. Log fills, realizations, triggers
```

### MAE Persistence Tracking

```python
# State tracking
mae_breach_start: Optional[int] = None  # When breach started
mae_values_in_breach: List[float] = []   # MAE values during breach

# Every trade update:
if mae >= 0.08:  # 8¢ threshold
    if breach just started:
        Record breach_start time
    else:
        Check duration = now - breach_start
        if duration >= 8s:
            TRIGGER! (persistent breach)
else:
    Reset breach tracking (price recovered)
```

---

## Key Classes

### `BotConfig`
Configuration container for all parameters

### `Fill`
Represents a single fill (layer in inventory)
- `qty`: Contracts
- `price`: Entry price (dollars)
- `timestamp`: Unix timestamp
- `side`: 'long' or 'short'

### `InventoryState`
Maintains inventory with FIFO realization
- `layers`: Deque of Fill objects
- `net_contracts`: Signed position (+ = long, - = short)
- `vwap_entry`: Volume-weighted average entry price
- `realized_pnl`: Total realized P&L

**Key methods:**
- `inventory_mae(price)`: Calculate MAE vs VWAP
- `duration_weighted_exposure(time)`: Calculate time×size risk
- `add_fill(qty, price, time, side)`: Add new fill
- `realize_pnl(qty, exit_price, time)`: Close position (FIFO)
- `flatten_all(exit_price, time)`: Close entire inventory

### `HybridRiskPolicy`
Risk management with persistence checking
- `should_flatten()`: Check if should exit
- `get_inventory_skew()`: Calculate quote bias

### `LiveMarketMaker`
Main bot orchestrator
- `on_trade(trade)`: Process incoming trade
- `get_status()`: Get current bot state
- `export_logs()`: Export fills/realizations/triggers to CSV

---

## Integration Guide

### Option 1: WebSocket Integration (Recommended)

```python
import websocket
import json

def on_message(ws, message):
    """Handle incoming WebSocket message."""
    data = json.loads(message)

    if data['type'] == 'trade':
        bot.on_trade({
            'timestamp': data['timestamp'],
            'price': data['price'],
            'taker_side': data['taker_side'],
            'count': data['count']
        })

# Connect to Kalshi WebSocket
ws = websocket.WebSocketApp(
    "wss://api.kalshi.com/trade-api/ws/v2",
    on_message=on_message
)
ws.run_forever()
```

### Option 2: REST API Polling

```python
import time

def poll_trades(bot, market_ticker):
    """Poll REST API for new trades."""
    last_timestamp = 0

    while True:
        # Fetch trades since last_timestamp
        trades = fetch_trades_from_api(
            market_ticker,
            min_timestamp=last_timestamp
        )

        for trade in trades:
            bot.on_trade(trade)
            last_timestamp = max(last_timestamp, trade['timestamp'])

        time.sleep(1)  # Poll every second
```

### Option 3: Supabase Real-time (Current Setup)

```python
def subscribe_to_trades(bot, market_ticker):
    """Subscribe to Supabase real-time trades."""

    def handle_insert(payload):
        trade = payload['new']
        if trade['market_ticker'] == market_ticker:
            bot.on_trade(trade)

    supabase.table('trades').on('INSERT', handle_insert).subscribe()
```

---

## Risk Management Details

### Position Sizing

```python
# Per-fill size
SIZE_PER_FILL = 10  # Fixed 10 contracts per fill

# Inventory limits
MAX_INVENTORY_VALUE = 100  # Max $100 notional per side

# Example:
# At 50¢ price: Max 200 contracts per side
# At 80¢ price: Max 125 contracts per side
```

### Spread Management

```python
# Base spread: 1¢
base_spread = 0.01

# Inventory skew adjustment
if net_contracts > 20 (long):
    quote_ask = mid + base_spread × (1 + skew)  # Widen ask
elif net_contracts < -20 (short):
    quote_bid = mid - base_spread × (1 + skew)  # Widen bid
```

### Fee Calculation

```python
# Kalshi fee: 1.75% × contracts × price × (1 - price)
fee = 0.0175 × 10 × 0.50 × (1 - 0.50) = $0.044

# Rounded up to nearest cent
fee = $0.05
```

---

## Monitoring & Logging

### Real-time Status

```python
status = bot.get_status()

# Returns:
{
    'net_contracts': -20,              # Short 20 contracts
    'vwap_entry': 0.5234,             # Entry VWAP
    'realized_pnl': 12.45,            # Realized P&L
    'mid_ema': 0.5189,                # Current mid price
    'trades_processed': 1523,         # Trades seen
    'fills_executed': 45,             # Our fills
    'policy_triggers': 3,             # Risk exits
    'duration_weighted_exposure': 342, # Current exposure
    'inventory_mae': 0.0045           # Current MAE (0.45¢)
}
```

### Log Export

```python
# Export logs to CSV
bot.export_logs(output_dir='logs')

# Creates:
# logs/MARKET_TICKER_fills_20251027_143052.csv
# logs/MARKET_TICKER_realizations_20251027_143052.csv
# logs/MARKET_TICKER_triggers_20251027_143052.csv
```

**Fill Log:**
- timestamp, qty, price, side, fee
- net_contracts_after, vwap_after

**Realization Log:**
- entry_price, exit_price, entry_time, exit_time
- hold_time_seconds, side, pnl, exit_reason

**Trigger Log:**
- trigger_reason, net_contracts_before, vwap_entry
- exit_price, pnl_before_flatten, pnl_from_flatten

---

## Common Scenarios

### Scenario 1: Goal Scored (Normal Volatility)

```
Time: 12:34:56 - Goal scored!
Price: 55¢ → 43¢ (12¢ drop in 2s)
Spread: 1¢ → 6¢

Bot inventory: Long 30 contracts @ VWAP 52¢
MAE: 52¢ - 43¢ = 9¢

MAE check: 9¢ > 8¢ threshold? YES
Persistence: Has it been >8s? NO (only 2s)
→ No trigger

Time: 12:35:04 (8s later)
Price: Recovered to 51¢
MAE: 52¢ - 51¢ = 1¢
→ Breach ended, reset tracking

Result: ✅ Survived goal volatility, captured mean reversion
```

### Scenario 2: Momentum Break (Tail Risk)

```
Time: 14:22:10 - Momentum break
Price: 55¢ → 38¢ (17¢ sustained drop)
Spread: 1¢ → 2¢ (normal)

Bot inventory: Long 40 contracts @ VWAP 52¢
MAE: 52¢ - 38¢ = 14¢

MAE check: 14¢ > 8¢ threshold? YES
Persistence tracking:
  t=0s: MAE 9¢ (breach starts)
  t=4s: MAE 12¢
  t=8s: MAE 14¢ → TRIGGER!

Action: Flatten entire inventory @ 38¢
Loss: 40 × (0.52 - 0.38) = -$5.60

Result: ✅ Circuit breaker activated, prevented larger loss
```

### Scenario 3: Duration-Weighted Exit

```
Time: Game start + 45 minutes
Inventory: Long 12 contracts @ VWAP 48¢ (held 42 min)

Duration-weighted exposure:
  12 contracts × 42 minutes = 504

Threshold: 500
→ Trigger: duration_weighted_504

Action: Flatten @ current mid 51¢
Profit: 12 × (0.51 - 0.48) = +$0.36

Result: ✅ Normal time-based exit, captured mean reversion
```

---

## Tuning Guide

### Conservative (Lower Risk)

```python
DURATION_WEIGHTED_LIMIT = 350       # Exit faster
MAE_FAILSAFE_CENTS = 6.0           # Tighter stop
MAE_ACTIVATION_WINDOW_SEC = 5      # Faster trigger
```

### Aggressive (Higher Return)

```python
DURATION_WEIGHTED_LIMIT = 700       # Hold longer
MAE_FAILSAFE_CENTS = 10.0          # Wider stop
MAE_ACTIVATION_WINDOW_SEC = 12     # More patience
```

### Goal-Event Optimized (Recommended)

```python
DURATION_WEIGHTED_LIMIT = 500       # Balanced
MAE_FAILSAFE_CENTS = 8.0           # 2σ above noise
MAE_ACTIVATION_WINDOW_SEC = 8      # Filters spikes
```

---

## Production Checklist

- [ ] Set correct `market_ticker` in main()
- [ ] Configure Supabase credentials in `config/settings.py`
- [ ] Integrate WebSocket or REST API feed
- [ ] Add order execution API (buy/sell orders)
- [ ] Set up monitoring/alerting
- [ ] Configure logging rotation
- [ ] Test with paper trading first
- [ ] Set position size limits per capital
- [ ] Add exchange connectivity health checks
- [ ] Implement graceful shutdown on disconnect

---

## Files

### Core Files
- `live_mm_bot.py` - Main bot code (this file)
- `README_LIVE_BOT.md` - This documentation

### Dependencies
- `config/settings.py` - Supabase credentials
- `supabase` - Python Supabase client

### Generated (at runtime)
- `logs/*_fills_*.csv` - Fill logs
- `logs/*_realizations_*.csv` - Realization logs
- `logs/*_triggers_*.csv` - Trigger logs

---

## FAQ

### Q: Why 8¢ MAE failsafe instead of 3¢?
**A:** NHL goal events create 8-12¢ volatility spikes routinely. 3¢ triggers on noise and cuts mean-reversion opportunities. 8¢ catches true momentum breaks (>10¢ sustained moves) while surviving normal volatility.

### Q: Why 8-second persistence window?
**A:** Goal-event price spikes typically recover within 5-8 seconds. The 8s window filters transient noise while catching sustained adverse moves.

### Q: Can I run multiple markets simultaneously?
**A:** Yes! Create one `LiveMarketMaker` instance per market. Each maintains independent inventory.

### Q: How do I handle settlement?
**A:** Call `bot.inventory.flatten_all(settlement_price, settlement_time)` when market settles. Settlement price = 1.0 (yes) or 0.0 (no).

### Q: What about fees?
**A:** Fees are calculated on both entry and exit. The bot uses Kalshi's fee formula: 1.75% × contracts × price × (1 - price), rounded up.

### Q: How much capital do I need?
**A:** With `MAX_INVENTORY_VALUE = 100`, you need ~$200-300 per market (accounting for both sides + margin).

---

## Support & Next Steps

1. **Test in simulation** - Run against historical trade data
2. **Paper trade** - Connect to live feed without executing
3. **Start small** - Deploy with 1-2 markets, low size
4. **Monitor closely** - Watch logs, track P&L vs backtest
5. **Scale gradually** - Increase markets and size slowly

**Contact:** Built for NHL goal-event volatility capture. Good luck! 🏒

---

## License

Internal use only. Do not redistribute.
