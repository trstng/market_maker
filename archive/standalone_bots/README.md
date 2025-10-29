# Archived Standalone Bots

This directory contains older standalone bot implementations that are **NOT** used by Railway deployment.

## Files

### `live_mm_bot.py`
- **Status**: Not deployed to Railway
- **Description**: Standalone market maker bot with its own config class
- **Note**: Accidentally received pyramiding config updates that were meant for `main_async.py`
- **Railway actually uses**: `main_async.py` + `multi_market_maker.py` + `market_book.py`

## Production Bot Architecture (Railway)

```
main_async.py (entry point)
├─ AsyncBotConfig (configuration)
├─ multi_market_maker.py (orchestrates multiple markets)
└─ market_book.py (core market-making logic)
```

These archived bots may still have useful code for reference, but they are not part of the production deployment.
