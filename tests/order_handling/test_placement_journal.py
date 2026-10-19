import re

from grid_trading_bot.core.order_handling.order import OrderSide, OrderType
from grid_trading_bot.core.order_handling.placement_journal import (
    PendingPlacement,
    PlacementJournal,
    PlacementPurpose,
    client_order_id_prefix,
    new_client_order_id,
)


def _entry(
    client_order_id: str,
    side: OrderSide = OrderSide.BUY,
    quantity: float = 0.5,
    price: float | None = 100.0,
    order_type: OrderType = OrderType.LIMIT,
    level_price: float | None = 100.0,
    purpose: PlacementPurpose = PlacementPurpose.GRID,
) -> PendingPlacement:
    return PendingPlacement(
        client_order_id=client_order_id,
        order_type=order_type,
        side=side,
        quantity=quantity,
        price=price,
        purpose=purpose,
        level_price=level_price,
    )


class TestClientOrderIds:
    def test_an_id_is_the_bot_prefix_then_sixteen_random_hex_characters(self):
        client_order_id = new_client_order_id("3f2a9c1d8e7b6a5f4e3d")

        assert re.fullmatch(r"g3f2a9c1d[0-9a-f]{16}", client_order_id)
        assert len(client_order_id) == 25

    def test_ids_never_repeat(self):
        assert len({new_client_order_id("3f2a9c1d") for _ in range(1000)}) == 1000

    def test_a_short_hash_is_padded_so_the_prefix_keeps_its_length(self):
        assert new_client_order_id("").startswith("g00000000")
        assert new_client_order_id("ab").startswith("gab000000")
        assert len(new_client_order_id("")) == 25

    def test_an_id_is_accepted_by_okx_and_binance(self):
        client_order_id = new_client_order_id("3f2a9c1d8e7b6a5f")

        assert re.fullmatch(r"[a-z0-9]{1,32}", client_order_id)
        assert re.fullmatch(r"[.A-Z:/a-z0-9_-]{1,36}", client_order_id)

    def test_every_id_starts_with_the_prefix_of_its_config(self):
        assert client_order_id_prefix("3f2a9c1d8e7b6a5f") == "g3f2a9c1d"
        assert client_order_id_prefix("ab") == "gab000000"
        assert new_client_order_id("3f2a9c1d8e7b6a5f").startswith(client_order_id_prefix("3f2a9c1d8e7b6a5f"))


class TestPlacementJournal:
    def test_a_new_entry_is_unsent_and_never_tried(self):
        entry = _entry("g1")

        assert (entry.sent, entry.attempts, entry.source_level_price) == (False, 0, None)

    def test_entries_come_back_in_creation_order(self):
        journal = PlacementJournal()
        for client_order_id in ("g3", "g1", "g2"):
            journal.add(_entry(client_order_id))

        assert [entry.client_order_id for entry in journal.entries()] == ["g3", "g1", "g2"]
        assert len(journal) == 3

    def test_an_entry_is_found_and_removed_by_its_client_order_id(self):
        journal = PlacementJournal()
        entry = _entry("g1")
        journal.add(entry)

        assert journal.get("g1") is entry
        assert journal.remove("g1") is entry
        assert journal.get("g1") is None
        assert len(journal) == 0

    def test_removing_an_unknown_entry_returns_none(self):
        assert PlacementJournal().remove("g-unknown") is None

    def test_market_entries_hold_no_level(self):
        journal = PlacementJournal()
        journal.add(_entry("g1", level_price=100.0))
        journal.add(_entry("g2", level_price=90.0))
        journal.add(
            _entry("g3", order_type=OrderType.MARKET, level_price=None, purpose=PlacementPurpose.INITIAL_PURCHASE)
        )

        assert [entry.client_order_id for entry in journal.limit_entries()] == ["g1", "g2"]
        assert [entry.client_order_id for entry in journal.pending_at(100.0)] == ["g1"]
        assert journal.pending_at(110.0) == []

    def test_reserved_is_what_the_limit_entries_hold(self):
        journal = PlacementJournal()
        journal.add(_entry("g1", OrderSide.BUY, quantity=0.5, price=100.0))
        journal.add(_entry("g2", OrderSide.BUY, quantity=0.2, price=90.0, level_price=90.0))
        journal.add(_entry("g3", OrderSide.SELL, quantity=0.3, price=120.0, level_price=120.0))
        journal.add(
            _entry(
                "g4",
                OrderSide.BUY,
                quantity=1.0,
                price=110.0,
                order_type=OrderType.MARKET,
                level_price=None,
                purpose=PlacementPurpose.INITIAL_PURCHASE,
            )
        )

        fiat, crypto = journal.reserved()

        assert fiat == 68.0
        assert crypto == 0.3

    def test_an_empty_journal_reserves_nothing(self):
        assert PlacementJournal().reserved() == (0.0, 0.0)

    def test_clear_empties_the_journal(self):
        journal = PlacementJournal()
        journal.add(_entry("g1"))

        journal.clear()

        assert journal.entries() == []
