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
        # Handle multiple newline formats (escaped, literal, etc.)
        pem_str = api_secret_pem

        # Replace escaped newlines (from environment variables)
        if '\\n' in pem_str:
            pem_str = pem_str.replace('\\n', '\n')

        # Ensure proper PEM format with newlines
        if '\n' not in pem_str and '-----BEGIN' in pem_str:
            # Single line PEM - add newlines
            import re
            pem_str = re.sub(r'-----BEGIN ([^-]+)-----', r'-----BEGIN \1-----\n', pem_str)
            pem_str = re.sub(r'-----END ([^-]+)-----', r'\n-----END \1-----', pem_str)
            # Split the middle part into 64-char lines
            parts = pem_str.split('\n')
            if len(parts) == 2:  # Just header and footer
                header = parts[0] + '\n'
                body_and_footer = parts[1]
                footer_match = re.search(r'(-----END [^-]+-----)', body_and_footer)
                if footer_match:
                    body = body_and_footer[:footer_match.start()]
                    footer = footer_match.group(1)
                    # Split body into 64-char lines
                    body_lines = [body[i:i+64] for i in range(0, len(body), 64)]
                    pem_str = header + '\n'.join(body_lines) + '\n' + footer

        pem_bytes = pem_str.encode('utf-8')

        try:
            self._priv = serialization.load_pem_private_key(
                pem_bytes,
                password=None,
                backend=default_backend()
            )
        except Exception as e:
            print(f"❌ Failed to load private key: {e}")
            print(f"Key starts with: {pem_str[:50]}...")
            raise

        # Async HTTP client
        self.http = httpx.AsyncClient(timeout=timeout)

    def _sig(self, ts: str, method: str, path: str) -> str:
        """
        Generate RSA-PSS signature for request.
        Per Kalshi docs: signature = timestamp + method + path (NO BODY)
        """
        msg = f"{ts}{method}{path}".encode()
        sig = self._priv.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        """Generate signed headers for request."""
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": self._sig(ts, method, path),
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
            # Signature must use FULL path from domain root
            sig_path = "/trade-api/v2/portfolio/orders"
            # But HTTP request uses relative path (base_url already has /trade-api/v2)
            req_path = "/portfolio/orders"

            payload = {
                "ticker": ticker,
                "client_order_id": f"{int(time.time() * 1000)}",
                "side": side,
                "action": action,
                "count": count,
                "type": "limit",
                "yes_price": price_cents if side == "yes" else None,
                "no_price": price_cents if side == "no" else None
            }

            # Serialize with sorted keys for consistent signature (no spaces)
            body = json.dumps(payload, separators=(',', ':'))

            r = await self.http.post(
                self.base_url + req_path,
                content=body,
                headers=self._headers("POST", sig_path)
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"❌ Order placement failed: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        try:
            sig_path = f"/trade-api/v2/portfolio/orders/{order_id}"
            req_path = f"/portfolio/orders/{order_id}"
            r = await self.http.delete(
                self.base_url + req_path,
                headers=self._headers("DELETE", sig_path)
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
            sig_path = f"/trade-api/v2/portfolio/orders/{order_id}"
            req_path = f"/portfolio/orders/{order_id}"
            r = await self.http.get(
                self.base_url + req_path,
                headers=self._headers("GET", sig_path)
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
            sig_path = "/trade-api/v2/portfolio/fills"
            req_path = "/portfolio/fills"
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor

            # Build query string
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            full_req_path = f"{req_path}?{query}"

            # Sign path WITHOUT query params (per Kalshi docs)
            headers = self._headers("GET", sig_path)

            r = await self.http.get(
                self.base_url + full_req_path,
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
            sig_path = "/trade-api/v2/markets"
            req_path = "/markets"

            # Build query string
            query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            full_req_path = f"{req_path}?{query}"

            # Sign path WITHOUT query params (per Kalshi docs)
            headers = self._headers("GET", sig_path)

            r = await self.http.get(
                self.base_url + full_req_path,
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
