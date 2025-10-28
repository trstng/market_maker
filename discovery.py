"""
Market Discovery
Auto-discovers today's markets for trading
"""
from datetime import datetime
from typing import List
from kalshi_async_client import KalshiAsyncClient


async def discover_today_markets(
    api: KalshiAsyncClient,
    series: str,
    limit: int = 100,
    top_k: int = 16,
    min_volume: int = 0
) -> List[str]:
    """
    Discover today's markets for a given series.

    Args:
        api: Kalshi async client
        series: Series ticker (e.g., 'KXNHLGAME')
        limit: Max markets to fetch from API
        top_k: Max markets to return
        min_volume: Minimum volume filter

    Returns:
        List of market tickers for today's games
    """
    print(f"🔍 Discovering markets for series: {series}")

    # Fetch open markets from API
    params = {
        "limit": limit,
        "status": "open",
        "series_ticker": series
    }

    data = await api.get_markets(params)
    markets = data.get("markets", [])

    print(f"📊 Found {len(markets)} open markets")

    # Filter for today's date (matches ticker format: KXNHLGAME-25OCT27...)
    today_str = datetime.now().strftime("%y%b%d").upper()  # e.g., "25OCT27"
    print(f"📅 Filtering for today: {today_str}")

    todays_markets = [
        m for m in markets
        if today_str in m.get("ticker", "")
    ]

    print(f"🎯 Found {len(todays_markets)} markets for today (including both teams)")

    # DEDUPLICATE: Each game has 2 markets (one per team)
    # Extract game identifier and pick the highest volume market per game
    # Ticker format: KXNHLGAME-25OCT28LASJ-LA
    # Game identifier: extract the matchup (LASJ = LA vs SJ)
    game_markets = {}
    for m in todays_markets:
        ticker = m.get("ticker", "")
        # Split ticker: ['KXNHLGAME', '25OCT28LASJ', 'LA']
        # The matchup is in the second part, after the date
        parts = ticker.split("-")
        if len(parts) >= 2:
            # Extract matchup from "25OCT28LASJ" -> need to remove date (first 7 chars)
            date_and_matchup = parts[1]
            if len(date_and_matchup) > 7:
                matchup = date_and_matchup[7:]  # e.g., "LASJ", "MTLSEA", "UTAEDM"

                # Keep the highest volume market for this game
                if matchup not in game_markets or m.get("volume", 0) > game_markets[matchup].get("volume", 0):
                    game_markets[matchup] = m

    todays_markets = list(game_markets.values())
    print(f"✅ After deduplication: {len(todays_markets)} unique games")

    # Apply volume filter if specified
    if min_volume > 0:
        todays_markets = [
            m for m in todays_markets
            if (m.get('volume') or 0) >= min_volume
        ]
        print(f"📈 After volume filter (>={min_volume}): {len(todays_markets)} markets")

    # Sort by volume (most active first)
    todays_markets.sort(key=lambda m: m.get('volume', 0), reverse=True)

    # Return top K tickers
    selected = [m['ticker'] for m in todays_markets[:top_k]]

    print(f"✅ Selected {len(selected)} markets for trading:")
    for i, ticker in enumerate(selected, 1):
        volume = next((m.get('volume', 0) for m in todays_markets if m['ticker'] == ticker), 0)
        print(f"   {i}. {ticker} (vol: {volume})")

    return selected
