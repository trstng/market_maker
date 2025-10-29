# 🚀 Edge-Locked Market Maker - Deployment Runbook

## ✅ PRE-FLIGHT: VALIDATION COMPLETE

```
🎉 ALL 5 CORE TESTS PASSED
✅ TP Math: 7¢ offset (VWAP $0.60 → TP $0.67)
✅ State Machine: FLAT/SKEW_LONG/SKEW_SHORT
✅ Price Gates: NHL 0.48/0.65
✅ MAE Trim: 28.1% at 11¢ MAE
✅ Circuit Breaker: Configured & adaptive
```

---

## 🎯 DEPLOYMENT SEQUENCE

### Phase 1: Paper Trading (Recommended Start)

**Run paper mode with real market data but NO real orders:**

```bash
# Terminal 1: Start bot in paper mode (1 market)
LIVE_TRADING_ENABLED=false \
MAX_MARKETS_TO_TRADE=1 \
python3 main_async.py 2>&1 | tee paper_mode.log
```

**What to watch for (first 5 minutes):**

1. **Startup Sanity Checks** - Should see:
   ```
   ==============================================================
   [TICKER] STARTUP SANITY CHECKS
   ==============================================================
   TP Math:
     Fees: 1.75¢ + 1.75¢ = 3.50¢
     TP Offset: 7¢
     Example: VWAP=$0.60 → TP_LONG=$0.67
   ```

2. **JSON Telemetry** - Every event logged as single-line JSON:
   ```json
   {"ts":1704..., "market":"NHL-...", "state":"FLAT", "action":"quote_place", ...}
   ```

3. **No errors** - Connection should establish, markets discovered

**Parse logs for anomalies:**

```bash
# In Terminal 2 (while bot running):

# Watch state changes
tail -f paper_mode.log | grep '"action":"state_change"'

# Watch quote placements
tail -f paper_mode.log | grep '"action":"quote_place"'

# Check for TP drift (should be constant between VWAP changes)
tail -f paper_mode.log | jq 'select(.action=="quote_place" and .state!="FLAT") | {market, vwap, tp, price}'
```

**Stop criteria (after 15-30 min if all looks good):**
- Ctrl+C to stop
- Review logs for any warnings/errors

---

### Phase 2: Shadow Mode (Optional - Test Logic Without Orders)

**Run with shadow mode to see what WOULD be quoted:**

```bash
SHADOW_MODE=true \
LIVE_TRADING_ENABLED=false \
MAX_MARKETS_TO_TRADE=1 \
python3 main_async.py 2>&1 | tee shadow_mode.log
```

**Watch for:**
- `{"action":"shadow_quote", ...}` - Proposed quotes logged
- `would_cross: false` - Quotes never cross spread
- Cancel rate projection < tier-1 thresholds

---

### Phase 3: Live Trading - Single Market (CAREFUL!)

**⚠️ ONLY proceed if paper mode looked clean**

```bash
# Terminal 1: Go live with 1 market
LIVE_TRADING_ENABLED=true \
MAX_MARKETS_TO_TRADE=1 \
MAX_INVENTORY_PER_MARKET=20 \
python3 main_async.py 2>&1 | tee live_1market.log
```

**Terminal 2: Monitor in real-time:**

```bash
# Watch fills
tail -f live_1market.log | grep -E '(FILL|REALIZE|sweet_spot_exit|state_change)'

# Emergency kill switch (if needed)
export DISABLE_NEW_QUOTES=true
# Bot will stop new quotes but allow positions to exit
```

**Success criteria (first hour):**
- Fills only in profitable zones (≤48¢ buy, ≥65¢ sell for NHL)
- TP stays at VWAP±7¢ (no drift when mid moves)
- State transitions work smoothly (FLAT→SKEW→FLAT)
- No circuit breaker false triggers

---

### Phase 4: Scale to Multiple Markets

**After 24-48 hours stable on 1 market:**

```bash
# Increase to 2 markets
LIVE_TRADING_ENABLED=true \
MAX_MARKETS_TO_TRADE=2 \
MAX_INVENTORY_PER_MARKET=20 \
python3 main_async.py 2>&1 | tee live_2markets.log
```

**After another 48 hours stable:**

```bash
# Full portfolio
LIVE_TRADING_ENABLED=true \
MAX_MARKETS_TO_TRADE=8 \
MAX_INVENTORY_PER_MARKET=50 \
python3 main_async.py 2>&1 | tee live_full.log
```

---

## 🛑 EMERGENCY CONTROLS

### Kill Switch (Stop New Quotes)

```bash
# In another terminal while bot running:
export DISABLE_NEW_QUOTES=true

# Bot will:
# - Stop placing new quotes immediately
# - Allow existing positions to exit normally
# - Respect exit windows (sweet-spot, hard-cap)
```

### Force Shutdown

```bash
# Ctrl+C in bot terminal
# Bot will:
# - Cancel all active orders
# - Gracefully shutdown
# - Positions remain (will exit on next run or manual close)
```

### Revert to Legacy Logic

```bash
# If new logic has issues:
export USE_LEGACY_LOGIC=true
python3 main_async.py

# Falls back to old inventory-skewing logic
```

---

## 📊 MONITORING DASHBOARDS

### Real-Time Alerts (Terminal 2)

```bash
# Alert on TP drift (should be rare/never)
tail -f live.log | jq 'select(.action=="quote_place" and .state!="FLAT" and (.tp - (.vwap + 0.07) | abs) > 0.005) | "\u001b[31mTP DRIFT: \(.market) vwap=\(.vwap) tp=\(.tp)\u001b[0m"'

# Alert on price gate breaches (should never happen)
tail -f live.log | jq 'select(.action=="FILL" and ((.price < 0.48) or (.price > 0.65))) | "\u001b[31mPRICE GATE BREACH: \(.market) \(.action) @ \(.price)\u001b[0m"'

# Alert on circuit breaker
tail -f live.log | grep -E '"(cb_soft|cb_hard)"' --color=always
```

### Daily Analysis

```bash
# Parse full day's logs
cat live.log | jq -s '
  group_by(.market) |
  map({
    market: .[0].market,
    fills: [.[] | select(.action=="FILL")] | length,
    exits: [.[] | select(.action | test("exit"))] | length,
    avg_hold_time: ([.[] | select(.action | test("exit")) | .age_s] | add / length),
    cb_triggers: [.[] | select(.action | test("cb_"))] | length
  })
'

# Expected output:
# {
#   "market": "NHL-LEAFS-WIN-B45",
#   "fills": 15,
#   "exits": 14,
#   "avg_hold_time": 612,  // ~10 min ✅
#   "cb_triggers": 0       // ✅
# }
```

---

## 🔍 FAILURE MODE CHECKLIST

### TP Drift Toward Mid
**Symptom:** TP changes when mid moves (no VWAP change)
**Check:** `grep state_change live.log` - should only see on fills
**Fix:** Critical bug - stop trading, review

### Cooldown Blocks Exits
**Symptom:** Exit signals logged but no orders sent
**Check:** `grep -A2 sweet_spot_exit live.log | grep quote_place`
**Fix:** Cooldown bypass not working - manual exit positions

### Skew Leak (Quotes on Wrong Side)
**Symptom:** BID quotes while SKEW_LONG
**Check:** `jq 'select(.state=="SKEW_LONG" and .side=="bid")' live.log`
**Fix:** State guard missing - stop immediately

### Circuit Breaker False Positives
**Symptom:** CB triggers during normal volatility
**Check:** `grep cb_soft live.log | jq .cancels_30` - review counts
**Fix:** If < 10 cancels/30s, thresholds too tight

---

## 📈 SUCCESS METRICS (24-Hour Checkpoint)

```bash
# Run after 24 hours live
cat live.log | jq -s '
{
  "total_fills": [.[] | select(.action=="FILL")] | length,
  "avg_hold_time_min": ([.[] | select(.age_s) | .age_s] | add / length) / 60,
  "exits_in_sweet_spot": [.[] | select(.action=="sweet_spot_exit")] | length,
  "cb_activations": [.[] | select(.action | test("cb_"))] | length,
  "fills_in_zone": [.[] | select(.action=="FILL" and ((.price <= 0.48) or (.price >= 0.65)))] | length
}
'
```

**Green Light Criteria:**
- ✅ `avg_hold_time_min`: 8-12 (in sweet-spot window)
- ✅ `exits_in_sweet_spot` > 50% of total exits
- ✅ `cb_activations`: 0-2 (rare/never)
- ✅ `fills_in_zone` ≥ 90% (most fills in profitable zones)

**Red Flags:**
- ❌ `avg_hold_time_min` > 30 (exits not firing)
- ❌ `cb_activations` > 5 (runaway loops)
- ❌ `fills_in_zone` < 70% (price gates failing)

---

## 🔄 ROLLBACK PROCEDURE

If issues arise:

```bash
# 1. Stop current bot
Ctrl+C

# 2. Tag current version (if not already done)
git tag v2.0-edge-locked-attempt1
git push origin v2.0-edge-locked-attempt1

# 3. Revert to pre-refactor code
git checkout v1.0-pre-refactor  # (create this tag first if not exists)

# 4. Restart with old logic
python3 main_async.py

# 5. Investigate issue offline
# Review logs, fix bug, re-test in paper mode
```

---

## 📝 CONFIGURATION REFERENCE

### Key Environment Variables

```bash
# Trading mode
LIVE_TRADING_ENABLED=true|false     # Real orders vs paper mode
SHADOW_MODE=true|false               # Log quotes without sending

# Inventory limits
MAX_MARKETS_TO_TRADE=1               # Number of markets
MAX_INVENTORY_PER_MARKET=20          # Contracts per market

# Kill switches
DISABLE_NEW_QUOTES=true|false        # Emergency stop
USE_LEGACY_LOGIC=true|false          # Fallback to old code
```

### Per-Market Overrides (in main_async.py)

```python
MARKET_THRESHOLDS = {
    "NHL": {"low": 0.48, "high": 0.65},
    "NFL": {"low": 0.44, "high": 0.72},
    "CFB": {"low": 0.42, "high": 0.75},
}
```

---

## ✅ DEPLOYMENT CHECKLIST

Before going live:

- [ ] Core tests passed (run `python3 test_core_logic.py`)
- [ ] Paper mode ran for 30+ min with no errors
- [ ] Startup log shows "TP Offset: 7¢"
- [ ] JSON telemetry is logging correctly
- [ ] Kill switch tested (`DISABLE_NEW_QUOTES=true`)
- [ ] API credentials configured in `.env`
- [ ] Discord webhook configured (optional)
- [ ] Git tag created: `v2.0-edge-locked`
- [ ] Backup of old code exists: `v1.0-pre-refactor`
- [ ] Monitoring terminals ready
- [ ] Emergency procedures understood

---

## 🎯 QUICK START (RECOMMENDED PATH)

```bash
# Step 1: Validate core logic (done ✅)
python3 test_core_logic.py

# Step 2: Paper mode - 30 minutes
LIVE_TRADING_ENABLED=false MAX_MARKETS_TO_TRADE=1 python3 main_async.py 2>&1 | tee paper.log

# Step 3: Review logs
grep -E '(STARTUP|TP Offset|state_change)' paper.log
cat paper.log | jq 'select(.action | test("exit|trim|cb_"))' | head -20

# Step 4: If clean, go live with 1 market
LIVE_TRADING_ENABLED=true MAX_MARKETS_TO_TRADE=1 MAX_INVENTORY_PER_MARKET=20 python3 main_async.py 2>&1 | tee live.log

# Step 5: Monitor for 24-48 hours, then scale
```

---

**🚀 You're ready to deploy! Start with paper mode and work up from there. Good luck!**
