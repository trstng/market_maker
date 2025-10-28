"""
Async Kalshi API Client
Uses httpx.AsyncClient for all API operations
"""
import time
import json
import base64
import httpx
from typing import Dict, Optional, List
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


class KalshiAsyncClient:
    """Async client for Kalshi REST API with RSA signature-based auth."""

    def __init__(self, base_url: str, ws_url: str, api_key: str, api_secret_pem: str, timeout: float = 30.0):
        self.base_url = base_url
        self.ws_url = ws_url
        self.api_key = api_key

        # Load private key for signing
        # Handle both raw PEM and escaped newlines
        if '\\n' in api_secret_pem:
            pem_bytes = api_secret_pem.replace('\\n', '\n').encode('utf-8')
        else:
            pem_bytes = api_secret_pem.encode('utf-8')

        self._priv = serialization.load_pem_private_key(
            pem_bytes,
            password=None,
            backend=default_backend()
        )

        # Async HTTP client
        self.http = httpx.AsyncClient(timeout=timeout)

    def _sig(self, ts: str, method: str, path: str, body: str = "") -> str:
        """Generate RSA-PSS signature for request."""
        msg = f"{ts}{method}{path}{body}".encode()
        sig = self._priv.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate signed headers for request."""
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": self._sig(ts, method, path, body),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json"
        }

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int
    ) -> Optional[Dict]:
        """Place a limit order."""
        try:
            path = "/portfolio/orders"
            payload = {
                "ticker": ticker,
                "client_order_id": f"{int(time.time() * 1000)}",
                "side": side,
                "action": action,
                "type": "limit",
                "yes_price": price_cents if side == "yes" else None,
                "no_price": price_cents if side == "no" else None
            }
            body = json.dumps(payload)

            r = await self.http.post(
                self.base_url + path,
                content=body,
                headers=self._headers("POST", path, body)
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"❌ Order placement failed: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        try:
            path = f"/portfolio/orders/{order_id}"
            r = await self.http.delete(
                self.base_url + path,
                headers=self._headers("DELETE", path)
            )
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"❌ Order cancellation failed: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Optional[Dict]:
        """
        Get status of a specific order.
        Returns None if order not found (404), Dict otherwise.
        """
        try:
            path = f"/portfolio/orders/{order_id}"
            r = await self.http.get(
                self.base_url + path,
                headers=self._headers("GET", path)
            )

            # Handle 404 specially (order executed/removed)
            if r.status_code == 404:
                return None

            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"❌ Get order status failed: {e}")
            return None

    async def get_fills(self, limit: int = 200, cursor: Optional[str] = None) -> Dict:
        """
        Get portfolio fills with pagination support.
        Returns: {fills: [...], cursor: "..."}
        """
        try:
            path = "/portfolio/fills"
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor

            # Build query string
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            full_path = f"{path}?{query}"

            # Sign the FULL path (including query string)
            headers = self._headers("GET", full_path)

            r = await self.http.get(
                self.base_url + full_path,
                headers=headers
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"❌ Get fills failed: {e}")
            return {"fills": [], "cursor": None}

    async def get_markets(self, params: Dict) -> Dict:
        """Get markets with filter parameters."""
        try:
            path = "/markets"

            # Build query string
            query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            full_path = f"{path}?{query}"

            # Sign the FULL path (including query string)
            headers = self._headers("GET", full_path)

            r = await self.http.get(
                self.base_url + full_path,
                headers=headers
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"❌ Get markets failed: {e}")
            return {"markets": []}

    async def close(self):
        """Close the HTTP client."""
        await self.http.aclose()
