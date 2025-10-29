# Archived Code

This directory is currently empty - all code is in active use.

**Note**: `live_mm_bot.py` was temporarily moved here but has been restored to root because:
- It contains shared utility classes (`HybridRiskPolicy`, `InventoryState`)
- `main_async.py` imports these classes
- It's needed as a utilities module, not just a standalone bot

## Production Bot Architecture (Railway)

```
main_async.py (entry point)
├─ AsyncBotConfig (configuration)
├─ multi_market_maker.py (orchestrates multiple markets)
├─ market_book.py (core market-making logic)
└─ live_mm_bot.py (shared utility classes)
```
