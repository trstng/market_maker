"""
Example Bot Configuration File

Copy this to bot_config.py and customize for your deployment.
"""

class BotConfig:
    """Live trading bot configuration."""

    # ========================================================================
    # SUPABASE CONNECTION
    # ========================================================================

    # Your Supabase credentials
    SUPABASE_URL = "https://your-project.supabase.co"
    SUPABASE_KEY = "your-anon-key-here"

    # ========================================================================
    # RISK POLICY PARAMETERS
    # ========================================================================

    # Duration-weighted exposure limit (PRIMARY GOVERNOR)
    # Formula: sum(contracts × age_minutes) for all open positions
    #
    # Examples:
    #   - 10 contracts × 50 minutes = 500 exposure
    #   - 50 contracts × 10 minutes = 500 exposure
    #
    # Recommended: 500 (balanced)
    # Conservative: 350 (faster exits)
    # Aggressive: 700 (longer holds)
    DURATION_WEIGHTED_LIMIT = 500

    # MAE failsafe circuit breaker (cents)
    # Triggers when price moves this many cents against VWAP entry
    # AND persists for MAE_ACTIVATION_WINDOW_SEC
    #
    # Why 8¢:
    #   - Goal events: 8-12¢ typical volatility (won't trigger)
    #   - Momentum break: 15¢+ sustained (will trigger)
    #
    # Recommended: 8.0 (2σ above noise)
    # Conservative: 6.0 (tighter stop)
    # Aggressive: 10.0 (wider stop)
    MAE_FAILSAFE_CENTS = 8.0

    # MAE activation window (seconds)
    # MAE must persist above threshold for this duration to trigger
    #
    # Why 8s:
    #   - Goal spikes typically recover within 5-8s
    #   - Filters transient noise
    #   - Catches sustained adverse moves
    #
    # Recommended: 8 (filters noise)
    # Conservative: 5 (faster response)
    # Aggressive: 12 (more patience)
    MAE_ACTIVATION_WINDOW_SEC = 8

    # ========================================================================
    # TRADING PARAMETERS
    # ========================================================================

    # Base bid/ask spread (dollars)
    # We quote at mid ± BASE_SPREAD
    #
    # Recommended: 0.01 (1 cent)
    # Tight: 0.005 (0.5 cents - high volume only)
    # Wide: 0.02 (2 cents - low volume)
    BASE_SPREAD = 0.01

    # Contracts per fill
    # How many contracts we buy/sell per executed fill
    #
    # Recommended: 10 (low risk)
    # Small: 5 (testing)
    # Medium: 20 (moderate risk)
    # Large: 50+ (high risk, requires capital)
    SIZE_PER_FILL = 10

    # Maximum inventory value per side (dollars)
    # Prevents over-exposure on one side
    #
    # Example at 50¢ price:
    #   MAX_INVENTORY_VALUE = 100 → Max 200 contracts
    #
    # Recommended: 100 (per market)
    # Conservative: 50
    # Aggressive: 200
    MAX_INVENTORY_VALUE = 100

    # Queue share assumption
    # What % of top-of-book volume we assume to capture
    #
    # Recommended: 0.2 (20% - realistic)
    # Conservative: 0.1 (10% - passive)
    # Aggressive: 0.5 (50% - requires fast execution)
    QUEUE_SHARE = 0.2

    # ========================================================================
    # MARKET SELECTION
    # ========================================================================

    # Which markets to trade
    # Options: 'NHL', 'NBA', 'NFL', 'CFB'
    SERIES_TICKER = 'NHL'

    # Minimum spread threshold (dollars)
    # Don't quote if market spread exceeds this
    # Prevents trading in illiquid/volatile conditions
    #
    # Recommended: 0.02 (2 cents)
    MIN_SPREAD_THRESHOLD = 0.02

    # ========================================================================
    # PRICE TRACKING
    # ========================================================================

    # EMA alpha for mid price smoothing
    # Higher = more reactive, Lower = more smooth
    #
    # Formula: new_ema = α × new_price + (1-α) × old_ema
    #
    # Recommended: 0.3 (balanced)
    # Smooth: 0.1
    # Reactive: 0.5
    MID_PRICE_EMA_ALPHA = 0.3

    # ========================================================================
    # LOGGING
    # ========================================================================

    # Enable/disable logging
    LOG_FILLS = True           # Log every fill
    LOG_REALIZATIONS = True    # Log every P&L realization
    LOG_TRIGGERS = True        # Log every policy trigger

    # Log output directory
    LOG_OUTPUT_DIR = 'logs'

    # ========================================================================
    # DATABASE TABLES
    # ========================================================================

    # Supabase table names (if you customized them)
    TRADES_TABLE = 'trades'
    MARKET_METADATA_TABLE = 'market_metadata'
    MARKET_SNAPSHOTS_TABLE = 'market_snapshots'

    # ========================================================================
    # ADVANCED SETTINGS
    # ========================================================================

    # Inventory skew threshold
    # Start adjusting quotes when |net_contracts| exceeds this
    INVENTORY_SKEW_THRESHOLD = 20

    # Maximum inventory cap (hard limit)
    # Bot will stop quoting if exceeded
    MAX_INVENTORY_CONTRACTS = 200

    # Position timeout (seconds)
    # Maximum age of any single fill before forced exit
    # Set to None to disable
    POSITION_TIMEOUT_SEC = None  # Disabled (use duration-weighted instead)


# ============================================================================
# PRESET CONFIGURATIONS
# ============================================================================

class ConservativeConfig(BotConfig):
    """Conservative configuration for low-risk trading."""
    DURATION_WEIGHTED_LIMIT = 350
    MAE_FAILSAFE_CENTS = 6.0
    MAE_ACTIVATION_WINDOW_SEC = 5
    SIZE_PER_FILL = 5
    MAX_INVENTORY_VALUE = 50
    BASE_SPREAD = 0.015


class AggressiveConfig(BotConfig):
    """Aggressive configuration for higher returns."""
    DURATION_WEIGHTED_LIMIT = 700
    MAE_FAILSAFE_CENTS = 10.0
    MAE_ACTIVATION_WINDOW_SEC = 12
    SIZE_PER_FILL = 20
    MAX_INVENTORY_VALUE = 200
    BASE_SPREAD = 0.008


class GoalEventConfig(BotConfig):
    """Optimized for NHL goal-event volatility capture."""
    DURATION_WEIGHTED_LIMIT = 500
    MAE_FAILSAFE_CENTS = 8.0
    MAE_ACTIVATION_WINDOW_SEC = 8
    SIZE_PER_FILL = 10
    MAX_INVENTORY_VALUE = 100
    BASE_SPREAD = 0.01
    SERIES_TICKER = 'NHL'


class HighFrequencyConfig(BotConfig):
    """High-frequency trading with tight spreads."""
    DURATION_WEIGHTED_LIMIT = 300
    MAE_FAILSAFE_CENTS = 5.0
    MAE_ACTIVATION_WINDOW_SEC = 5
    SIZE_PER_FILL = 10
    MAX_INVENTORY_VALUE = 150
    BASE_SPREAD = 0.005
    MID_PRICE_EMA_ALPHA = 0.5  # More reactive


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

"""
Example 1: Use default config
-------------------------------
from bot_config import BotConfig
config = BotConfig()


Example 2: Use preset
---------------------
from bot_config import GoalEventConfig
config = GoalEventConfig()


Example 3: Custom override
---------------------------
from bot_config import BotConfig

class MyConfig(BotConfig):
    DURATION_WEIGHTED_LIMIT = 450
    MAE_FAILSAFE_CENTS = 7.5
    SIZE_PER_FILL = 15

config = MyConfig()


Example 4: Runtime override
----------------------------
from bot_config import BotConfig

config = BotConfig()
config.DURATION_WEIGHTED_LIMIT = 600  # Override at runtime
"""
