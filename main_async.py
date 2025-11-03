"""
Async Multi-Market Bot - Main Entry Point
Discovers today's markets and trades them all simultaneously
"""
import asyncio
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed. Install with: pip install python-dotenv")
    print("⚠️  Attempting to use environment variables directly...")

from config.settings import settings
from multi_market_maker import MultiMarketMaker
from discovery import discover_today_markets
from kalshi_async_client import KalshiAsyncClient

# Import your existing policy and inventory classes
from live_mm_bot import HybridRiskPolicy, InventoryState


class AsyncBotConfig:
    """Configuration for async multi-market bot."""

    # Kalshi API connection
    KALSHI_API_KEY = settings.kalshi_api_key
    KALSHI_API_SECRET = settings.kalshi_api_secret
    KALSHI_BASE_URL = settings.kalshi_base_url
    KALSHI_WS_URL = settings.kalshi_base_url.replace("https://", "wss://").replace("/trade-api/v2", "/trade-api/ws/v2")

    # Trading parameters (from your backtested config)
    BASE_SPREAD = settings.base_spread
    SIZE_PER_FILL = settings.size_per_fill
    MAX_INVENTORY_VALUE = settings.max_inventory_value
    QUEUE_SHARE = settings.queue_share

    # Risk policy parameters (unchanged from backtests)
    DURATION_WEIGHTED_LIMIT = settings.duration_weighted_limit
    MAE_FAILSAFE_CENTS = settings.mae_failsafe_cents
    MAE_ACTIVATION_WINDOW_SEC = settings.mae_activation_window_sec

    # Market selection
    SERIES_TICKER = settings.series_ticker
    MIN_SPREAD_THRESHOLD = settings.min_spread_threshold

    # EMA smoothing
    MID_PRICE_EMA_ALPHA = 0.3

    # Discord webhook
    DISCORD_WEBHOOK_URL = getattr(settings, 'discord_webhook_url', None)

    # Portfolio governors (new)
    MAX_HOT_MARKETS = getattr(settings, 'max_hot_markets', 8)
    MAX_MARKETS_TO_TRADE = getattr(settings, 'max_markets_to_trade', 2)  # Start with 2 for testing

    # ======================================================================
    # NEW: Edge-Locked Market Making Parameters
    # ======================================================================

    # Fee & TP calculation
    TP_MIN_EDGE_C = 2  # Minimum edge in cents (after fees)
    MAKER_FEE_C = 1.75  # Approximate maker fee in cents
    EXIT_FEE_C = 1.75  # Approximate exit fee in cents
    TICK_C = 1  # Tick size in cents

    # Price-selective gating (per-market overrides)
    DEFAULT_PRICE_ENTRY_LOW = 0.48  # Only buy below this price
    DEFAULT_PRICE_ENTRY_HIGH = 0.65  # Only sell above this price
    MARKET_THRESHOLDS = {
        "NHL": {"low": 0.48, "high": 0.65},
        "NFL": {"low": 0.44, "high": 0.72},
        "CFB": {"low": 0.42, "high": 0.75},
    }

    # Hysteresis & cooldown
    HYSTERESIS_TICKS = 4  # Minimum tick displacement before replacing order
    BASE_COOLDOWN_FLAT_MS = 300  # Cooldown when flat (ms)
    BASE_COOLDOWN_SKEW_MS = 2000  # Base cooldown when holding inventory (ms)
    COOLDOWN_PER_CONTRACT_MS = 50  # Additional cooldown per contract held
    MAX_COOLDOWN_MS = 5000  # Maximum cooldown cap

    # Circuit breaker (2-tier, rolling windows)
    CB_TIER1_30S = 12  # Soft brake: 12 cancels in 30s
    CB_TIER1_60S = 18  # Soft brake: 18 cancels in 60s
    CB_TIER2_30S = 16  # Hard brake: 16 cancels in 30s
    CB_TIER2_60S = 24  # Hard brake: 24 cancels in 60s
    CB_SOFT_BRAKE_S = 12  # Soft brake duration (seconds)
    CB_HARD_BRAKE_S = 30  # Hard brake duration (seconds)
    CB_SKEW_MULTIPLIER = 1.25  # Relax thresholds by 25% when holding inventory
    CB_TIGHT_SPREAD_MULTIPLIER = 0.8  # Tighten thresholds by 20% when spread is 1 tick

    # Pyramiding controls (position accumulation)
    ALLOW_PYRAMIDING = False          # DISABLED to match backtest (backtest has NO pyramiding)
    PYRAMID_MAX_CONTRACTS = 20        # Match backtest cap
    PYRAMID_SIZE_PER_FILL = 2         # Contracts per layer (backtest shows qty=2)
    PYRAMID_STEP_C = 2                # Grid step in cents between layers
    PYRAMID_REENTRY_COOLDOWN_S = 8    # Seconds between re-entry fills
    PYRAMID_USE_VWAP_GRID = True      # True=grid from VWAP, False=from last fill

    # Backtest-matching neutral zone
    NEUTRAL_ZONE_THRESHOLD = 20       # Quote both sides when abs(net) < this (backtest uses 20)

    # ======================================================================
    # Paper Trading Guardrails & Safety Features
    # ======================================================================

    # Shadow mode (log proposed quotes without sending to exchange)
    SHADOW_MODE = os.getenv('SHADOW_MODE', 'false').lower() == 'true'

    # Inventory limits
    MAX_INVENTORY_PER_MARKET = getattr(settings, 'max_inventory_per_market', 50)
    MAX_GLOBAL_INVENTORY = getattr(settings, 'max_global_inventory', 100)
    MAX_TIME_AT_RISK_S = 60 * 60  # 60 minutes (stricter than 40m hard cap for safety)

    # Kill switches (can be toggled via env vars)
    DISABLE_NEW_QUOTES = os.getenv('DISABLE_NEW_QUOTES', 'false').lower() == 'true'
    USE_LEGACY_LOGIC = os.getenv('USE_LEGACY_LOGIC', 'false').lower() == 'true'


async def main():
    """Main entry point for async multi-market bot."""
    print("=" * 60)
    print("🏒 KALSHI MULTI-MARKET BOT - Async Edition")
    print("=" * 60)

    # Load configuration
    config = AsyncBotConfig()

    # Validate settings
    try:
        settings.validate()
    except ValueError as e:
        print(f"❌ Configuration error: {e}")
        return

    print(f"\n📊 Configuration:")
    print(f"   Series: {config.SERIES_TICKER}")
    print(f"   Base Spread: {config.BASE_SPREAD}")
    print(f"   Size Per Fill: {config.SIZE_PER_FILL}")
    print(f"   Max Markets: {config.MAX_MARKETS_TO_TRADE}")
    print(f"   Discord: {'✅ Enabled' if config.DISCORD_WEBHOOK_URL else '❌ Disabled'}")

    # Create temp API client for discovery
    print("\n🔍 Discovering today's markets...")
    temp_api = KalshiAsyncClient(
        base_url=config.KALSHI_BASE_URL,
        ws_url=config.KALSHI_WS_URL,
        api_key=config.KALSHI_API_KEY,
        api_secret_pem=config.KALSHI_API_SECRET
    )

    try:
        # Discover all markets for today
        tickers = await discover_today_markets(
            api=temp_api,
            series=config.SERIES_TICKER,
            limit=100,
            top_k=config.MAX_MARKETS_TO_TRADE,
            min_volume=0  # No minimum volume filter
        )

        if not tickers:
            print("❌ No markets found for today. Exiting.")
            await temp_api.close()
            return

        print(f"\n✅ Will trade {len(tickers)} markets")

    finally:
        await temp_api.close()

    # Create and run multi-market maker
    print("\n🚀 Starting multi-market maker...")
    mm = MultiMarketMaker(
        config=config,
        tickers=tickers,
        policy_cls=HybridRiskPolicy,
        inventory_cls=InventoryState
    )

    try:
        await mm.run()
    except KeyboardInterrupt:
        print("\n⚠️  Keyboard interrupt - shutting down...")
        await mm.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
