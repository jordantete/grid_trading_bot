from dataclasses import dataclass
from enum import Enum
import secrets
from typing import TYPE_CHECKING

from .order import Order, OrderSide, OrderStatus, OrderType

if TYPE_CHECKING:
    from ..grid_management.grid_level import GridLevel
    from ..grid_management.grid_manager import GridManager
    from .order_book import OrderBook


class PlacementPurpose(Enum):
    GRID = "grid"
    PAIRED = "paired"
    REPLACEMENT = "replacement"
    INITIAL_PURCHASE = "initial_purchase"
    LIQUIDATION = "liquidation"


@dataclass
class PendingPlacement:
    """One placement the bot has not settled yet: written to the database before it is sent, kept until resolved."""

    client_order_id: str
    order_type: OrderType
    side: OrderSide
    quantity: float
    price: float | None
    purpose: PlacementPurpose
    level_price: float | None = None
    source_level_price: float | None = None
    # The filled (PAIRED) or ended (REPLACEMENT) order this placement follows, so a restart does not follow it twice.
    source_order_id: str | None = None
    sent: bool = False
    created_at: int = 0
    attempts: int = 0


class PlacementJournal:
    """The placements whose outcome the bot has not settled yet, in creation order."""

    def __init__(self) -> None:
        self._entries: dict[str, PendingPlacement] = {}

    def add(self, entry: PendingPlacement) -> None:
        self._entries[entry.client_order_id] = entry

    def remove(self, client_order_id: str) -> PendingPlacement | None:
        return self._entries.pop(client_order_id, None)

    def get(self, client_order_id: str) -> PendingPlacement | None:
        return self._entries.get(client_order_id)

    def entries(self) -> list[PendingPlacement]:
        return list(self._entries.values())

    def limit_entries(self) -> list[PendingPlacement]:
        return [entry for entry in self._entries.values() if entry.order_type == OrderType.LIMIT]

    def pending_at(self, level_price: float) -> list[PendingPlacement]:
        """The limit entries aimed at the level priced `level_price`."""
        return [entry for entry in self.limit_entries() if entry.level_price == level_price]

    def reserved(self) -> tuple[float, float]:
        """The fiat and the crypto the limit entries hold reserved; market entries reserve nothing."""
        limits = self.limit_entries()
        fiat = sum(entry.quantity * entry.price for entry in limits if entry.side == OrderSide.BUY)
        crypto = sum(entry.quantity for entry in limits if entry.side == OrderSide.SELL)
        return float(fiat), float(crypto)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def client_order_id_prefix(config_hash: str) -> str:
    """What every client order id of this config starts with: `g`, then the config's 8-character hash prefix."""
    return "g" + config_hash[:8].ljust(8, "0")


def new_client_order_id(config_hash: str) -> str:
    """A fresh id for one placement: the config's prefix, then 16 random hex characters."""
    return client_order_id_prefix(config_hash) + secrets.token_hex(8)


def adopt_order(
    entry: PendingPlacement,
    order: Order,
    grid_manager: "GridManager",
    order_book: "OrderBook",
) -> "GridLevel | None":
    """
    Puts an order the exchange holds for `entry` into the book, open with nothing booked, on the entry's level and
    paired with its source level. What the order already filled reaches the pipeline as an update.
    """
    order.status = OrderStatus.OPEN
    order.filled = 0.0
    order.remaining = order.amount
    grid_level = grid_manager.grid_levels.get(entry.level_price) if entry.level_price is not None else None
    if grid_level is not None:
        grid_manager.mark_order_pending(grid_level, order)
    order_book.add_order(order, grid_level)
    source_level = (
        grid_manager.grid_levels.get(entry.source_level_price) if entry.source_level_price is not None else None
    )
    if grid_level is not None and source_level is not None:
        grid_manager.pair_grid_levels(source_level, grid_level, pairing_type=order.side.value)
    return grid_level
