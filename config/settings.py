import os
from typing import Optional


class Settings:
    """Bot settings loaded from environment variables"""

    def __init__(self):
        # Supabase Configuration
        self.supabase_url: str = os.getenv('SUPABASE_URL', '')
        self.supabase_key: str = os.getenv('SUPABASE_KEY', '')

        # Kalshi API Configuration
        self.kalshi_api_key: str = os.getenv('KALSHI_API_KEY', '')
        self.kalshi_api_secret: str = os.getenv('KALSHI_API_SECRET', '')
        self.kalshi_environment: str = os.getenv('KALSHI_ENVIRONMENT', 'production')

        # Trading Parameters (with backtest defaults)
        self.duration_weighted_limit: int = int(os.getenv('DURATION_WEIGHTED_LIMIT', '500'))
        self.mae_failsafe_cents: float = float(os.getenv('MAE_FAILSAFE_CENTS', '8.0'))
        self.mae_activation_window_sec: int = int(os.getenv('MAE_ACTIVATION_WINDOW_SEC', '8'))
        self.base_spread: float = float(os.getenv('BASE_SPREAD', '0.01'))
        self.size_per_fill: int = int(os.getenv('SIZE_PER_FILL', '10'))
        self.max_inventory_value: int = int(os.getenv('MAX_INVENTORY_VALUE', '100'))
        self.series_ticker: str = os.getenv('SERIES_TICKER', 'NHL')

        # Additional bot parameters
        self.queue_share: float = float(os.getenv('QUEUE_SHARE', '0.20'))
        self.min_spread_threshold: float = float(os.getenv('MIN_SPREAD_THRESHOLD', '0.02'))

    def validate(self) -> bool:
        """Validate that required settings are present"""
        errors = []

        if not self.supabase_url:
            errors.append("SUPABASE_URL is required")
        if not self.supabase_key:
            errors.append("SUPABASE_KEY is required")
        if not self.kalshi_api_key:
            errors.append("KALSHI_API_KEY is required")
        if not self.kalshi_api_secret:
            errors.append("KALSHI_API_SECRET is required")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

        return True

    def __repr__(self):
        return f"<Settings series={self.series_ticker} env={self.kalshi_environment}>"


# Global settings instance
settings = Settings()
