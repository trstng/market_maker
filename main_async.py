"""
Async Multi-Market Bot - Main Entry Point
Discovers today's markets and trades them all simultaneously
"""
import asyncio
import sys
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
