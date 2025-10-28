"""
Order State Management
Tracks order lifecycle and provides idempotent fill processing
"""
from typing import Dict, Optional, Set, Tuple


class OrderState:
    """
    Manages order state and provides idempotent fill processing.

    Tracks:
    - Active bid/ask orders
    - Client order ID mapping
    - Processed fills (for deduplication)
    - Fill cursor for restart recovery
    """

    def __init__(self):
        # Map client_order_id to order details
        self.client_map: Dict[str, Dict] = {}

        # Active orders on each side
        self.active_bid: Optional[Dict] = None  # {'order_id', 'client_order_id', 'price', 'qty', 'filled_count'}
        self.active_ask: Optional[Dict] = None  # {'order_id', 'client_order_id', 'price', 'qty', 'filled_count'}

        # Order registry: order_id -> metadata (for routing)
        self.order_registry: Dict[str, Dict] = {}

        # Processed fills: (order_id, fill_id) pairs
        self.processed_fills: Set[Tuple[str, str]] = set()

        # Cursor for fill pagination/resume
        self.last_fill_cursor: Optional[str] = None

    def already_processed(self, order_id: str, fill_id: str) -> bool:
        """
        Check if a fill has already been processed (idempotent).

        Args:
            order_id: Order ID from fill
            fill_id: Fill ID from fill

        Returns:
            True if already processed, False otherwise (and marks as processed)
        """
        key = (order_id, fill_id)
        if key in self.processed_fills:
            return True
        self.processed_fills.add(key)
        return False

    def register_order(self, client_order_id: str, order_details: Dict):
        """Register a placed order for tracking."""
        self.client_map[client_order_id] = order_details

    def clear_active_bid(self):
        """Clear active bid state."""
        self.active_bid = None

    def clear_active_ask(self):
        """Clear active ask state."""
        self.active_ask = None

    def get_order_count(self) -> int:
        """Get count of tracked orders."""
        return len(self.client_map)
