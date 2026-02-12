import pytest

from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType


class TestOrder:
    @pytest.fixture
    def sample_order(self):
        return Order(
            identifier="123",
            status=OrderStatus.OPEN,
            order_type=OrderType.LIMIT,
            side=OrderSide.BUY,
            price=1000.0,
            average=None,
            amount=5.0,
            filled=0.0,
            remaining=5.0,
            timestamp=1695890800,
            datetime="2024-01-01T00:00:00Z",
            last_trade_timestamp=None,
            symbol="BTC/USDT",
            time_in_force="GTC",
        )

    def test_create_order_with_valid_data(self, sample_order):
        assert sample_order.identifier == "123"
        assert sample_order.status == OrderStatus.OPEN
        assert sample_order.order_type == OrderType.LIMIT
        assert sample_order.side == OrderSide.BUY
        assert sample_order.price == 1000.0
        assert sample_order.amount == 5.0
        assert sample_order.filled == 0.0
        assert sample_order.remaining == 5.0
        assert sample_order.timestamp == 1695890800
        assert sample_order.datetime == "2024-01-01T00:00:00Z"
        assert sample_order.symbol == "BTC/USDT"
        assert sample_order.time_in_force == "GTC"

    def test_is_filled(self, sample_order):
        sample_order.status = OrderStatus.CLOSED
        assert sample_order.is_filled() is True
        sample_order.status = OrderStatus.OPEN
        assert sample_order.is_filled() is False

    def test_is_canceled(self, sample_order):
        sample_order.status = OrderStatus.CANCELED
        assert sample_order.is_canceled() is True
        sample_order.status = OrderStatus.OPEN
        assert sample_order.is_canceled() is False

    def test_is_open(self, sample_order):
        sample_order.status = OrderStatus.OPEN
        assert sample_order.is_open() is True
        sample_order.status = OrderStatus.CLOSED
        assert sample_order.is_open() is False

    def test_format_last_trade_timestamp(self, sample_order):
        # Case 1: No last trade timestamp
        assert sample_order.format_last_trade_timestamp() is None

        # Case 2: Valid last trade timestamp
        sample_order.last_trade_timestamp = 1695890800
        assert sample_order.format_last_trade_timestamp() == "2023-09-28T08:46:40"

    def test_order_str_representation(self, sample_order):
        order_str = str(sample_order)
        assert "Order(id=123, status=OrderStatus.OPEN" in order_str
        assert "type=OrderType.LIMIT, side=OrderSide.BUY, price=1000.0" in order_str

    def test_order_repr_representation(self, sample_order):
        order_repr = repr(sample_order)
        assert order_repr == str(sample_order)

    def test_order_with_trades_and_fee(self):
        order = Order(
            identifier="456",
            status=OrderStatus.CLOSED,
            order_type=OrderType.LIMIT,
            side=OrderSide.SELL,
            price=2000.0,
            average=1950.0,
            amount=3.0,
            filled=3.0,
            remaining=0.0,
            timestamp=1695890800,
            datetime="2024-01-01T00:00:00Z",
            last_trade_timestamp=1695890900,
            symbol="ETH/USDT",
            time_in_force="GTC",
            trades=[
                {"id": "trade1", "price": 1950.0, "amount": 1.0},
                {"id": "trade2", "price": 1950.0, "amount": 2.0},
            ],
            fee={"currency": "USDT", "cost": 5.0},
            cost=5850.0,
        )
        assert order.is_filled() is True
        assert order.fee == {"currency": "USDT", "cost": 5.0}
        assert order.trades == [
            {"id": "trade1", "price": 1950.0, "amount": 1.0},
            {"id": "trade2", "price": 1950.0, "amount": 2.0},
        ]
        assert order.cost == 5850.0
