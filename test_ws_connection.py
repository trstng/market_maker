"""Quick test of WebSocket connection"""
import asyncio
import json
import time
import websockets
from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from kalshi_async_client import KalshiAsyncClient

async def test_ws():
    api = KalshiAsyncClient(
        base_url=settings.kalshi_base_url,
        ws_url=settings.kalshi_base_url.replace("https://", "wss://").replace("/trade-api/v2", "/trade-api/ws/v2"),
        api_key=settings.kalshi_api_key,
        api_secret_pem=settings.kalshi_api_secret
    )

    # Generate WS headers
    path = "/trade-api/ws/v2"
    ts = str(int(time.time() * 1000))
    sig = api._sig(ts, "GET", path, "")
    headers = {
        "KALSHI-ACCESS-KEY": settings.kalshi_api_key,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts
    }

    print(f"Connecting to: {api.ws_url}")
    print(f"Headers: {list(headers.keys())}")

    try:
        async with websockets.connect(
            api.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10
        ) as ws:
            print("✅ WebSocket connected!")

            # Subscribe to one market
            await ws.send(json.dumps({
                "id": 1,
                "cmd": "subscribe",
                "params": {"channels": ["ticker"]}
            }))
            print("📡 Subscribed to ticker channel")

            # Wait for messages
            for i in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)
                print(f"📨 Message {i+1}: {data.get('type', 'unknown')}")

            print("✅ Test successful!")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test_ws())
