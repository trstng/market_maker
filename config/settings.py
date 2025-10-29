import os
from typing import Optional


class Settings:
    """Bot settings loaded from environment variables"""

    def __init__(self):
        # Kalshi API Configuration
        self.kalshi_api_key: str = os.getenv('KALSHI_API_KEY', '')
        self.kalshi_api_secret: str = os.getenv('KALSHI_API_SECRET', '')
        self.kalshi_base_url: str = os.getenv('KALSHI_BASE_URL', 'https://api.elections.kalshi.com/trade-api/v2')

        # Trading Parameters (with backtest defaults)
        self.duration_weighted_limit: int = int(os.getenv('DURATION_WEIGHTED_LIMIT', '500'))
        self.mae_failsafe_cents: float = float(os.getenv('MAE_FAILSAFE_CENTS', '8.0'))
        self.mae_activation_window_sec: int = int(os.getenv('MAE_ACTIVATION_WINDOW_SEC', '8'))
        self.base_spread: float = float(os.getenv('BASE_SPREAD', '0.01'))
        self.size_per_fill: int = int(os.getenv('SIZE_PER_FILL', '10'))
        self.max_inventory_value: int = int(os.getenv('MAX_INVENTORY_VALUE', '100'))
        self.series_ticker: str = os.getenv('SERIES_TICKER', 'KXNHLGAME')

        # Additional bot parameters
        self.queue_share: float = float(os.getenv('QUEUE_SHARE', '0.20'))
        self.min_spread_threshold: float = float(os.getenv('MIN_SPREAD_THRESHOLD', '0.02'))

        # Discord webhook for alerts
        self.discord_webhook_url: Optional[str] = os.getenv('DISCORD_WEBHOOK_URL', None)

        # Trading mode and limits (for Railway deployment)
        self.live_trading_enabled: bool = os.getenv('LIVE_TRADING_ENABLED', 'true').lower() == 'true'
        self.max_markets_to_trade: int = int(os.getenv('MAX_MARKETS_TO_TRADE', '2'))
        self.max_inventory_per_market: int = int(os.getenv('MAX_INVENTORY_PER_MARKET', '50'))
        self.max_global_inventory: int = int(os.getenv('MAX_GLOBAL_INVENTORY', '100'))
        self.max_hot_markets: int = int(os.getenv('MAX_HOT_MARKETS', '8'))

    def validate(self) -> bool:
        """Validate that required settings are present"""
        errors = []

        if not self.kalshi_api_key:
            errors.append("KALSHI_API_KEY is required")
        if not self.kalshi_api_secret:
            errors.append("KALSHI_API_SECRET is required")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

        return True

    def __repr__(self):
        return f"<Settings series={self.series_ticker} base_url={self.kalshi_base_url}>"


# Global settings instance
settings = Settings()
