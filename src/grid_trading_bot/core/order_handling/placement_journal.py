from dataclasses import dataclass
from enum import Enum
import secrets

from .order import OrderSide, OrderType


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
