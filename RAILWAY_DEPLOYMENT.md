# Railway Deployment Guide

## Environment Variables for Railway

Set these environment variables in your Railway project dashboard to enable live trading with the edge-locked market maker:

### Required Variables (Already Set)
```bash
KALSHI_API_KEY=your_api_key_here
KALSHI_API_SECRET=your_pem_key_here
KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2
```

### New Trading Mode Variables (Add These)

#### Live Trading Control
```bash
LIVE_TRADING_ENABLED=true              # Set to 'true' for real orders, 'false' for paper mode
```

####Portfolio Limits
```bash
MAX_MARKETS_TO_TRADE=1                 # Start with 1 market for safety
MAX_INVENTORY_PER_MARKET=20            # Max contracts per market (start conservative)
MAX_GLOBAL_INVENTORY=100               # Max total contracts across all markets
MAX_HOT_MARKETS=8                      # Max markets to monitor simultaneously
```

### Optional Safety Controls
```bash
SHADOW_MODE=false                      # Set to 'true' to log quotes without sending (testing)
DISABLE_NEW_QUOTES=false               # Emergency kill switch - stops new quotes
USE_LEGACY_LOGIC=false                 # Fallback to old inventory-skewing logic
```

### Optional Discord Notifications
```bash
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## Deployment Steps

### 1. Update Environment Variables in Railway

**IMPORTANT: Start Conservative**

For initial live deployment, set these in Railway:
```bash
LIVE_TRADING_ENABLED=true
MAX_MARKETS_TO_TRADE=1
MAX_INVENTORY_PER_MARKET=20
```

### 2. Deploy the New Code

Railway will automatically deploy when you push to `main`. The latest commit includes:
- Edge-locked VWAP-anchored market maker
- State machine (FLAT/SKEW_LONG/SKEW_SHORT)
- Fee-aware TP calculation (7¢ offset)
- Price-selective gating
- Adaptive MAE trimming
- 2-tier circuit breaker

### 3. Monitor the Deployment

After Railway redeploys:
```bash
# View logs in Railway dashboard
# Watch for startup messages:
#   - "STARTUP SANITY CHECKS"
#   - "TP Offset: 7¢"
#   - "Entry Low: $0.48 (only buy below)"
#   - "Entry High: $0.65 (only sell above)"
```

### 4. Verify Live Trading Mode

Look for this in Railway logs:
```
[TICKER] 💰 LIVE MODE - Real orders will be placed
```

If you see:
```
[TICKER] 📄 PAPER MODE - No real orders will be placed
```

Then `LIVE_TRADING_ENABLED` is not set to `true`.

### 5. Emergency Controls

If you need to stop trading immediately:

**Option 1: Kill Switch (Graceful)**
```bash
# Set in Railway dashboard:
DISABLE_NEW_QUOTES=true
```
Bot will stop placing new quotes but allow existing positions to exit normally.

**Option 2: Switch to Paper Mode**
```bash
# Set in Railway dashboard:
LIVE_TRADING_ENABLED=false
```
Bot will continue running but won't place real orders.

**Option 3: Revert to Legacy Logic**
```bash
# Set in Railway dashboard:
USE_LEGACY_LOGIC=true
```
Falls back to old inventory-skewing logic if new logic has issues.

**Option 4: Stop Deployment**
Stop the Railway service entirely from the dashboard.

---

## Scaling Up

After 24-48 hours of stable operation with 1 market:

### Phase 1 → Phase 2 (2 markets)
```bash
MAX_MARKETS_TO_TRADE=2
MAX_INVENTORY_PER_MARKET=20
```

### Phase 2 → Phase 3 (Full portfolio)
After another 48 hours stable:
```bash
MAX_MARKETS_TO_TRADE=8
MAX_INVENTORY_PER_MARKET=50
```

---

## Monitoring Checklist

Check Railway logs for:
- [ ] Startup sanity checks appear
- [ ] TP offset is 7¢
- [ ] "LIVE MODE" message appears
- [ ] No error messages in first 5 minutes
- [ ] JSON telemetry logging correctly
- [ ] Market discovery successful
- [ ] WebSocket connection stable

---

## Troubleshooting

### Bot won't place orders
- Check `LIVE_TRADING_ENABLED=true` is set
- Verify mid price is outside profitable zones (NHL: <$0.48 or >$0.65)
- Check circuit breaker hasn't triggered

### Bot crashes on startup
- Verify `KALSHI_API_KEY` and `KALSHI_API_SECRET` are correct
- Check PEM key format (should start with `-----BEGIN PRIVATE KEY-----`)
- Review Railway logs for specific error

### Want to test without risk
```bash
LIVE_TRADING_ENABLED=false
SHADOW_MODE=true
```
Bot will log all proposed quotes but won't send any orders.

---

## Quick Reference: Recommended Settings

### Testing (Paper Mode)
```bash
LIVE_TRADING_ENABLED=false
MAX_MARKETS_TO_TRADE=1
```

### Initial Live Deployment (Conservative)
```bash
LIVE_TRADING_ENABLED=true
MAX_MARKETS_TO_TRADE=1
MAX_INVENTORY_PER_MARKET=20
```

### Full Production (After Validation)
```bash
LIVE_TRADING_ENABLED=true
MAX_MARKETS_TO_TRADE=8
MAX_INVENTORY_PER_MARKET=50
MAX_GLOBAL_INVENTORY=400
```
