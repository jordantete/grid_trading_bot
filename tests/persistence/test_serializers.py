from decimal import Decimal
import json
from unittest.mock import MagicMock

from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.persistence.serializers import (
    balance_to_dict,
    dict_to_order,
    grid_level_to_dict,
    order_to_dict,
)


class TestOrderSerialization:
    """Tests for order_to_dict and dict_to_order round-trip serialization."""

    def _make_order(self, **overrides):
        """Helper to create an Order with sensible defaults, allowing field overrides."""
        defaults = {
            "identifier": "order-abc-123",
            "status": OrderStatus.CLOSED,
            "order_type": OrderType.LIMIT,
            "side": OrderSide.BUY,
            "price": 42000.50,
            "average": 41998.25,
            "amount": 0.5,
            "filled": 0.5,
            "remaining": 0.0,
            "timestamp": 1700000000000,
            "datetime": "2023-11-14T22:13:20.000Z",
            "last_trade_timestamp": 1700000005000,
            "symbol": "BTC/USDT",
            "time_in_force": "GTC",
            "trades": [
                {"id": "t1", "price": 41998.25, "amount": 0.3},
                {"id": "t2", "price": 41998.25, "amount": 0.2},
            ],
            "fee": {"currency": "USDT", "cost": 10.50},
            "cost": 20999.125,
            "info": {"raw_exchange_field": "some_value", "nested": {"a": 1}},
        }
        defaults.update(overrides)
        return Order(**defaults)

    def test_order_round_trip_all_fields_populated(self):
        """Serializing an Order with all fields and deserializing it back yields an equivalent Order."""
        original = self._make_order()
        grid_level_price = 42000.0
        is_non_grid = False

        serialized = order_to_dict(original, grid_level_price, is_non_grid)
        restored = dict_to_order(serialized)

        assert restored.identifier == original.identifier
        assert restored.status == original.status
        assert restored.order_type == original.order_type
        assert restored.side == original.side
        assert restored.price == original.price
        assert restored.average == original.average
        assert restored.amount == original.amount
        assert restored.filled == original.filled
        assert restored.remaining == original.remaining
        assert restored.timestamp == original.timestamp
        assert restored.datetime == original.datetime
        assert restored.last_trade_timestamp == original.last_trade_timestamp
        assert restored.symbol == original.symbol
        assert restored.time_in_force == original.time_in_force
        assert restored.cost == original.cost
        assert restored.trades == original.trades
        assert restored.fee == original.fee
        assert restored.info == original.info

    def test_order_round_trip_with_none_optional_fields(self):
        """Serializing an Order where optional fields are None and deserializing it back preserves None values."""
        original = self._make_order(
            average=None,
            last_trade_timestamp=None,
            time_in_force=None,
            trades=None,
            fee=None,
            cost=None,
            info=None,
        )
        serialized = order_to_dict(original, grid_level_price=None, is_non_grid=True)
        restored = dict_to_order(serialized)

        assert restored.average is None
        assert restored.last_trade_timestamp is None
        assert restored.time_in_force is None
        assert restored.trades is None
        assert restored.fee is None
        assert restored.cost is None
        assert restored.info is None

    def test_order_to_dict_grid_level_metadata(self):
        """order_to_dict correctly stores grid_level_price and is_non_grid_order."""
        order = self._make_order()

        result_grid = order_to_dict(order, grid_level_price=42000.0, is_non_grid=False)
        assert result_grid["grid_level_price"] == 42000.0
        assert result_grid["is_non_grid_order"] == 0

        result_non_grid = order_to_dict(order, grid_level_price=None, is_non_grid=True)
        assert result_non_grid["grid_level_price"] is None
        assert result_non_grid["is_non_grid_order"] == 1

    def test_order_to_dict_enum_values_are_strings(self):
        """Enum fields are stored as their string values, not Enum objects."""
        order = self._make_order(
            status=OrderStatus.OPEN,
            order_type=OrderType.MARKET,
            side=OrderSide.SELL,
        )
        serialized = order_to_dict(order, grid_level_price=None, is_non_grid=False)

        assert serialized["status"] == "open"
        assert serialized["order_type"] == "market"
        assert serialized["side"] == "sell"

    def test_json_fields_serialize_and_deserialize_correctly(self):
        """trades, fee, and info fields survive JSON serialization/deserialization with correct types."""
        trades = [
            {"id": "trade-1", "price": 100.5, "amount": 1.0},
            {"id": "trade-2", "price": 101.0, "amount": 2.5},
        ]
        fee = {"currency": "USDT", "cost": 0.75}
        info = {"exchange_id": "binance", "nested": {"depth": 2, "value": "test"}}

        order = self._make_order(trades=trades, fee=fee, info=info)
        serialized = order_to_dict(order, grid_level_price=None, is_non_grid=False)

        # Verify JSON fields are stored as strings
        assert isinstance(serialized["trades_json"], str)
        assert isinstance(serialized["fee_json"], str)
        assert isinstance(serialized["info_json"], str)

        # Verify they parse back to the original structures
        assert json.loads(serialized["trades_json"]) == trades
        assert json.loads(serialized["fee_json"]) == fee
        assert json.loads(serialized["info_json"]) == info

        # Verify full round-trip through dict_to_order
        restored = dict_to_order(serialized)
        assert restored.trades == trades
        assert restored.fee == fee
        assert restored.info == info

    def test_json_fields_none_stored_as_none(self):
        """When trades, fee, and info are None, they are stored as None (not JSON 'null')."""
        order = self._make_order(trades=None, fee=None, info=None)
        serialized = order_to_dict(order, grid_level_price=None, is_non_grid=False)

        assert serialized["trades_json"] is None
        assert serialized["fee_json"] is None
        assert serialized["info_json"] is None


class TestGridLevelSerialization:
    """Tests for grid_level_to_dict serialization."""

    def test_grid_level_with_paired_levels(self):
        """grid_level_to_dict includes paired buy and sell level prices when present."""
        level = GridLevel(price=42000.0, state=GridCycleState.READY_TO_SELL)
        buy_level = GridLevel(price=41000.0, state=GridCycleState.READY_TO_BUY)
        sell_level = GridLevel(price=43000.0, state=GridCycleState.WAITING_FOR_SELL_FILL)

        level.paired_buy_level = buy_level
        level.paired_sell_level = sell_level

        result = grid_level_to_dict(level)

        assert result["price"] == 42000.0
        assert result["state"] == GridCycleState.READY_TO_SELL.value
        assert result["paired_buy_level_price"] == 41000.0
        assert result["paired_sell_level_price"] == 43000.0

    def test_grid_level_without_paired_levels(self):
        """grid_level_to_dict sets paired level prices to None when no paired levels exist."""
        level = GridLevel(price=42000.0, state=GridCycleState.READY_TO_BUY)

        result = grid_level_to_dict(level)

        assert result["price"] == 42000.0
        assert result["state"] == GridCycleState.READY_TO_BUY.value
        assert result["paired_buy_level_price"] is None
        assert result["paired_sell_level_price"] is None

    def test_grid_level_state_stored_as_string(self):
        """The state field in the serialized dict is the enum's string value."""
        for cycle_state in GridCycleState:
            level = GridLevel(price=100.0, state=cycle_state)
            result = grid_level_to_dict(level)
            assert result["state"] == cycle_state.value
            assert isinstance(result["state"], str)


class TestBalanceSerialization:
    """Tests for balance_to_dict serialization."""

    def test_balance_serialization_preserves_decimal_precision(self):
        """balance_to_dict converts internal Decimal fields to strings, preserving full precision."""
        tracker = MagicMock()
        tracker._balance = Decimal("12345.67890123456789")
        tracker._crypto_balance = Decimal("0.50000000")
        tracker._total_fees = Decimal("3.14159265358979323846")
        tracker._reserved_fiat = Decimal("500.00")
        tracker._reserved_crypto = Decimal("0.12345678")

        result = balance_to_dict(tracker)

        # Verify round-trip: parsing back to Decimal gives the original value
        assert Decimal(result["fiat_balance"]) == Decimal("12345.67890123456789")
        assert Decimal(result["crypto_balance"]) == Decimal("0.50000000")
        assert Decimal(result["total_fees"]) == Decimal("3.14159265358979323846")
        assert Decimal(result["reserved_fiat"]) == Decimal("500.00")
        assert Decimal(result["reserved_crypto"]) == Decimal("0.12345678")

    def test_balance_serialization_all_values_are_strings(self):
        """All values in the returned dict are strings, not Decimal or float."""
        tracker = MagicMock()
        tracker._balance = Decimal("1000")
        tracker._crypto_balance = Decimal("2")
        tracker._total_fees = Decimal("0")
        tracker._reserved_fiat = Decimal("0")
        tracker._reserved_crypto = Decimal("0")

        result = balance_to_dict(tracker)

        for key, value in result.items():
            assert isinstance(value, str), f"Expected str for key '{key}', got {type(value)}"

    def test_balance_serialization_zero_values(self):
        """balance_to_dict handles zero Decimal values correctly."""
        tracker = MagicMock()
        tracker._balance = Decimal("0")
        tracker._crypto_balance = Decimal("0")
        tracker._total_fees = Decimal("0")
        tracker._reserved_fiat = Decimal("0")
        tracker._reserved_crypto = Decimal("0")

        result = balance_to_dict(tracker)

        assert result["fiat_balance"] == "0"
        assert result["crypto_balance"] == "0"
        assert result["total_fees"] == "0"
        assert result["reserved_fiat"] == "0"
        assert result["reserved_crypto"] == "0"

    def test_balance_serialization_returns_expected_keys(self):
        """balance_to_dict returns exactly the expected set of keys."""
        tracker = MagicMock()
        tracker._balance = Decimal("100")
        tracker._crypto_balance = Decimal("1")
        tracker._total_fees = Decimal("0.5")
        tracker._reserved_fiat = Decimal("10")
        tracker._reserved_crypto = Decimal("0.1")

        result = balance_to_dict(tracker)

        expected_keys = {"fiat_balance", "crypto_balance", "total_fees", "reserved_fiat", "reserved_crypto"}
        assert set(result.keys()) == expected_keys
