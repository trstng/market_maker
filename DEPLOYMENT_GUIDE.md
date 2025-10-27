# Railway Deployment Guide

Complete guide for deploying the Kalshi Market Making Bot to Railway with hot reload from GitHub.

---

## Prerequisites

1. **GitHub Account**: Repository at `https://github.com/trstng/market_maker.git`
2. **Railway Account**: Sign up at https://railway.app
3. **Kalshi API Credentials**: API key and secret from Kalshi
4. **Supabase Database**: URL and API key for trade data storage

---

## Step 1: Push Code to GitHub

```bash
cd /Users/tgonz/Desktop/Kalshi/market_maker

# Initialize git (if not already done)
git init

# Add all files
git add .

# Commit
git commit -m "Initial commit: Kalshi market making bot with Railway deployment"

# Add remote
git remote add origin https://github.com/trstng/market_maker.git

# Push to GitHub
git push -u origin main
```

---

## Step 2: Create Railway Project

1. Go to https://railway.app/dashboard
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Choose `trstng/market_maker` repository
5. Railway will automatically detect:
   - `Procfile` → Start command
   - `requirements.txt` → Python dependencies
   - `railway.json` → Deployment configuration

---

## Step 3: Configure Environment Variables in Railway

In Railway dashboard, go to your project → **Variables** tab and add:

### Required Variables

```bash
# Supabase Configuration
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-anon-or-service-key

# Kalshi API Configuration
KALSHI_API_KEY=your-kalshi-api-key
KALSHI_API_SECRET=your-kalshi-api-secret
KALSHI_ENVIRONMENT=production
```

### Optional Trading Parameters (with defaults shown)

```bash
# Market Configuration
MARKET_TICKER=KXNHLGAME-25OCT30-TEAM
SERIES_TICKER=NHL

# Risk Parameters (backtest-proven values)
DURATION_WEIGHTED_LIMIT=500
MAE_FAILSAFE_CENTS=8.0
MAE_ACTIVATION_WINDOW_SEC=8

# Position Sizing
SIZE_PER_FILL=10
MAX_INVENTORY_VALUE=100
BASE_SPREAD=0.01
QUEUE_SHARE=0.20
MIN_SPREAD_THRESHOLD=0.02
```

---

## Step 4: Deploy

Railway will automatically deploy after you set the environment variables.

### Deployment Process:
1. Railway detects changes
2. Builds Docker container with Python 3.9+
3. Installs dependencies from `requirements.txt`
4. Runs `python live_mm_bot.py` (from Procfile)
5. Bot starts and connects to Kalshi WebSocket

### Monitor Deployment:
- Go to **Deployments** tab to see build logs
- Check **Logs** tab for runtime output
- Look for these success messages:
  ```
  ✅ Authenticated with Kalshi API
  ✅ WebSocket connected
  📡 Subscribed to MARKET-TICKER
  ✅ Bot initialized. Ready to trade.
  ```

---

## Step 5: Enable Hot Reload

Hot reload is automatic! Railway watches your GitHub repository.

**To deploy changes:**
1. Make code changes locally
2. Commit and push to GitHub:
   ```bash
   git add .
   git commit -m "Update trading parameters"
   git push
   ```
3. Railway automatically detects the push and redeploys

**Configuration:**
- Railway redeploys on every push to `main` branch
- Restart policy: `ON_FAILURE` with max 10 retries
- Deployment time: ~2-5 minutes

---

## Step 6: Monitor the Bot

### View Logs in Real-Time

In Railway dashboard → **Logs** tab:

```
[FILL] LONG 10 @ $0.5500 | Net: +10 | VWAP: $0.5500
[STATUS] Net: +10 | P&L: $0.00 | Trades: 45 | Fills: 1 | Triggers: 0
[REALIZE] Closed 10 @ $0.5600 | P&L: +$0.95 | Reason: normal_exit
[TRIGGER] duration_weighted_500 | Flattened +20 contracts @ $0.5550 | P&L: +$0.80
```

### Key Metrics to Watch:

- **Net Contracts**: Current inventory position
- **Realized P&L**: Total profit/loss
- **Fills Executed**: Number of market making fills
- **Policy Triggers**: How often risk policy exits positions

### Railway Monitoring:

- **CPU Usage**: Should be minimal (< 10%)
- **Memory**: Typically < 100MB
- **Network**: WebSocket connection active
- **Restarts**: Should be 0 (unless you redeploy)

---

## Trading Strategy Summary

### Strategy Overview:
- **Type**: Hybrid market making with duration-weighted risk control
- **Primary Exit**: Duration-weighted exposure ≥ 500 (contracts × minutes)
- **Failsafe**: MAE 8¢ circuit breaker (must persist 8 seconds)
- **Target Markets**: NHL goal-event volatility

### Position Management:
- **Entry**: Quote bid/ask at mid ± 1¢ spread
- **Size**: 10 contracts per fill
- **Inventory**: Max $100 per side
- **Exit**: Normal exit when offsetting fill, or policy trigger

### Risk Controls:
1. **Duration-Weighted Exit**: Prevents long-term directional exposure
   - Example: 10 contracts × 50 minutes = 500 → EXIT
2. **MAE Failsafe**: Catches sustained momentum breaks
   - Threshold: 8¢ adverse move from VWAP
   - Persistence: Must last 8 seconds to trigger
3. **Inventory Skew**: Widens quotes when net position > 20 contracts

---

## Troubleshooting

### Bot Won't Start

**Check environment variables:**
```bash
# In Railway dashboard → Variables tab
# Ensure all REQUIRED variables are set:
- SUPABASE_URL
- SUPABASE_KEY
- KALSHI_API_KEY
- KALSHI_API_SECRET
```

**Check logs for error:**
```
❌ Configuration Error: KALSHI_API_KEY is required
```

### WebSocket Disconnects

**Symptoms:**
```
⚠️ WebSocket closed: 1000 - Normal closure
❌ WebSocket error: Connection refused
```

**Solutions:**
- Check Kalshi API credentials are correct
- Verify `KALSHI_ENVIRONMENT` is set to `production` or `demo`
- Railway restarts automatically on disconnect (max 10 retries)

### No Fills Being Executed

**Possible causes:**
1. **Market ticker doesn't exist**: Check `MARKET_TICKER` environment variable
2. **No trading activity**: Market may be inactive
3. **Spread too tight**: Quotes not competitive
4. **Inventory maxed out**: Check `MAX_INVENTORY_VALUE`

**Debug:**
- Check logs for `[STATUS]` messages showing trade count
- Verify WebSocket is receiving data: `📡 Subscribed to...`
- Check `SERIES_TICKER` matches your intended markets

### Deployment Fails

**Check Railway build logs:**
```
# Common issues:
1. requirements.txt missing dependency
2. Python version incompatible (need 3.9+)
3. Syntax error in code
```

**Fix:**
1. Review deployment logs in Railway dashboard
2. Fix error locally
3. Push to GitHub → Railway auto-redeploys

---

## Stopping the Bot

### Temporary Stop:
- Railway dashboard → Project → **Pause**
- Bot stops but environment variables persist

### Permanent Stop:
- Railway dashboard → Project → **Delete**
- All data and logs are deleted

### Graceful Shutdown:
Bot handles `Ctrl+C` / Railway restart gracefully:
1. Disconnects WebSocket
2. Exports logs to CSV files
3. Exits cleanly

**Note:** With `START_FRESH_ON_RESTART` strategy, inventory state is NOT persisted.

---

## Cost Estimate

### Railway Pricing:
- **Free Tier**: $5/month credit (sufficient for light trading)
- **Pro Tier**: $20/month (unlimited usage)

### Bot Resource Usage:
- **CPU**: < 5% utilization
- **Memory**: ~80-100MB
- **Network**: Minimal (WebSocket + REST API calls)

**Estimated cost:** $0-5/month on free tier

---

## Advanced Configuration

### Change Trading Parameters Mid-Session

1. Go to Railway → Variables
2. Update parameter (e.g., `DURATION_WEIGHTED_LIMIT=600`)
3. Railway auto-restarts bot with new values
4. Bot starts fresh with new parameters

### Add Health Monitoring

**Option 1: Railway built-in metrics**
- Dashboard shows CPU, memory, network

**Option 2: External monitoring (future enhancement)**
- Integrate Datadog/Sentry/New Relic
- Add health check endpoint
- Alert on policy triggers

### Multiple Markets

To trade multiple markets simultaneously:

**Option 1: Multiple Railway projects**
- Deploy separate instance per market
- Set different `MARKET_TICKER` for each

**Option 2: Modify bot to handle multiple tickers (requires code changes)**
- Subscribe to multiple WebSocket channels
- Track separate inventory per market

---

## Next Steps

1. ✅ Deploy to Railway
2. ✅ Verify bot connects and starts trading
3. Monitor performance for 1-2 days
4. Adjust parameters based on P&L
5. Consider adding:
   - Health check endpoint
   - Monitoring integration
   - Multi-market support
   - Paper trading mode toggle

---

## Support

**Issues?**
- Check Railway logs first
- Review `.env.example` for correct environment variables
- Ensure Kalshi API credentials are valid
- Verify Supabase database is accessible

**Bot behavior questions?**
- Review `README_LIVE_BOT.md` for strategy details
- Check `strategy_recap.md` for policy explanations
- Examine backtest results in `backtests/` folder

---

## Security Notes

- ✅ `.gitignore` prevents committing secrets
- ✅ Use Railway environment variables (not hardcoded)
- ✅ Supabase keys should be service keys (not anon keys for production)
- ✅ Kalshi API keys should have minimum required permissions
- ⚠️ Never commit `.env` files to GitHub
- ⚠️ Rotate API keys periodically

---

## GitHub → Railway Auto-Deploy Workflow

```
Local Changes
    ↓
git add . && git commit -m "message"
    ↓
git push origin main
    ↓
GitHub receives push
    ↓
Railway detects new commit
    ↓
Railway builds new Docker image
    ↓
Railway runs tests (if configured)
    ↓
Railway deploys new version
    ↓
Bot restarts with new code
    ↓
WebSocket reconnects
    ↓
Bot resumes trading
```

**Total time:** ~2-5 minutes from push to live

---

## Checklist Before Going Live

- [ ] GitHub repository created and code pushed
- [ ] Railway project created and connected to GitHub
- [ ] All required environment variables set in Railway
- [ ] Bot deployed successfully (check deployment logs)
- [ ] WebSocket connected (check for ✅ messages in logs)
- [ ] Bot subscribed to correct market ticker
- [ ] Trading parameters match your risk tolerance
- [ ] Test with small position sizes first (`SIZE_PER_FILL=5`)
- [ ] Monitor for at least 1 hour before leaving unattended
- [ ] Verify P&L calculations are correct
- [ ] Check policy triggers are working as expected

---

**Ready to deploy!** 🚀
