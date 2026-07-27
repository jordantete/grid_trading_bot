from unittest.mock import Mock

import pytest

from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.order_handling.order_book import OrderBook


class TestOrderBook:
    @pytest.fixture
    def setup_order_book(self):
        return OrderBook()

    def test_add_order_with_grid(self, setup_order_book):
        order_book = setup_order_book
        buy_order = Mock(spec=Order, side=OrderSide.BUY)
        sell_order = Mock(spec=Order, side=OrderSide.SELL)
        grid_level = Mock(spec=GridLevel)

        order_book.add_order(buy_order, grid_level)
        order_book.add_order(sell_order, grid_level)

        assert len(order_book.buy_orders) == 1
        assert len(order_book.sell_orders) == 1
        assert order_book.order_to_grid_map[buy_order] == grid_level
        assert order_book.order_to_grid_map[sell_order] == grid_level

    def test_add_order_without_grid(self, setup_order_book):
        order_book = setup_order_book
        non_grid_order = Mock(spec=Order, side=OrderSide.SELL)

        order_book.add_order(non_grid_order)

        assert len(order_book.non_grid_orders) == 1
        assert order_book.non_grid_orders[0] == non_grid_order

    def test_get_buy_orders_with_grid(self, setup_order_book):
        order_book = setup_order_book
        buy_order = Mock(spec=Order, side=OrderSide.BUY)
        grid_level = Mock(spec=GridLevel)

        order_book.add_order(buy_order, grid_level)
        result = order_book.get_buy_orders_with_grid()

        assert len(result) == 1
        assert result[0] == (buy_order, grid_level)

    def test_get_sell_orders_with_grid(self, setup_order_book):
        order_book = setup_order_book
        sell_order = Mock(spec=Order, side=OrderSide.SELL)
        grid_level = Mock(spec=GridLevel)

        order_book.add_order(sell_order, grid_level)
        result = order_book.get_sell_orders_with_grid()

        assert len(result) == 1
        assert result[0] == (sell_order, grid_level)

    def test_get_all_buy_orders(self, setup_order_book):
        order_book = setup_order_book
        buy_order_1 = Mock(spec=Order, side=OrderSide.BUY)
        buy_order_2 = Mock(spec=Order, side=OrderSide.BUY)

        order_book.add_order(buy_order_1)
        order_book.add_order(buy_order_2)
        result = order_book.get_all_buy_orders()

        assert len(result) == 2
        assert buy_order_1 in result
        assert buy_order_2 in result

    def test_get_all_sell_orders(self, setup_order_book):
        order_book = setup_order_book
        sell_order_1 = Mock(spec=Order, side=OrderSide.SELL)
        sell_order_2 = Mock(spec=Order, side=OrderSide.SELL)

        order_book.add_order(sell_order_1)
        order_book.add_order(sell_order_2)
        result = order_book.get_all_sell_orders()

        assert len(result) == 2
        assert sell_order_1 in result
        assert sell_order_2 in result

    def test_get_open_orders(self, setup_order_book):
        order_book = setup_order_book
        open_order = Mock(spec=Order, side=OrderSide.BUY, is_open=Mock(return_value=True))
        closed_order = Mock(spec=Order, side=OrderSide.SELL, is_open=Mock(return_value=False))

        order_book.add_order(open_order)
        order_book.add_order(closed_order)
        result = order_book.get_open_orders()

        assert len(result) == 1
        assert open_order in result

    def test_get_completed_orders(self, setup_order_book):
        order_book = setup_order_book
        completed_order = Mock(spec=Order, side=OrderSide.BUY, is_filled=Mock(return_value=True))
        pending_order = Mock(spec=Order, side=OrderSide.BUY, is_filled=Mock(return_value=False))

        order_book.add_order(completed_order)
        order_book.add_order(pending_order)
        result = order_book.get_completed_orders()

        assert len(result) == 1
        assert completed_order in result

    def test_get_grid_level_for_order(self, setup_order_book):
        order_book = setup_order_book
        order = Mock(spec=Order, side=OrderSide.BUY)
        grid_level = Mock(spec=GridLevel)

        order_book.add_order(order, grid_level)
        result = order_book.get_grid_level_for_order(order)

        assert result == grid_level

    def test_update_order_status(self, setup_order_book):
        order_book = setup_order_book
        order = Mock(spec=Order, identifier="order_123", side=OrderSide.BUY, status=OrderStatus.OPEN)

        order_book.add_order(order)
        order_book.update_order_status("order_123", OrderStatus.CLOSED)

        assert order.status == OrderStatus.CLOSED

    def test_update_order_status_nonexistent_order(self, setup_order_book):
        order_book = setup_order_book
        order = Mock(spec=Order, identifier="order_123", status=OrderStatus.OPEN)
        order.side = OrderSide.BUY

        order_book.add_order(order)
        order_book.update_order_status("nonexistent_order", OrderStatus.CLOSED)

        assert order.status == OrderStatus.OPEN  # Ensure no changes for non-existent orders


def _make_order(identifier="ord-1", side=OrderSide.BUY, status=OrderStatus.OPEN):
    return Order(
        identifier=identifier,
        status=status,
        order_type=OrderType.LIMIT,
        side=side,
        price=100.0,
        average=None,
        amount=1.0,
        filled=0.0,
        remaining=1.0,
        timestamp=0,
        datetime=None,
        last_trade_timestamp=None,
        symbol="SOL/USDT",
        time_in_force="GTC",
    )


class TestRemoteOrderLookup:
    def test_get_grid_level_falls_back_to_identifier(self):
        book = OrderBook()
        grid_level = GridLevel(price=100.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        local = _make_order(identifier="abc")
        book.add_order(local, grid_level)
        remote = _make_order(identifier="abc")  # distinct object, same exchange id
        assert book.get_grid_level_for_order(remote) is grid_level

    def test_get_grid_level_identity_stays_primary(self):
        book = OrderBook()
        gl1 = GridLevel(price=100.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        gl2 = GridLevel(price=110.0, state=GridCycleState.WAITING_FOR_BUY_FILL)
        first = _make_order(identifier="dup")
        second = _make_order(identifier="dup")  # backtest ids can collide
        book.add_order(first, gl1)
        book.add_order(second, gl2)
        assert book.get_grid_level_for_order(first) is gl1
        assert book.get_grid_level_for_order(second) is gl2

    def test_remove_open_order_falls_back_to_identifier(self):
        book = OrderBook()
        local = _make_order(identifier="abc")
        book.add_order(local)
        remote = _make_order(identifier="abc")
        book.remove_open_order(remote)
        assert local not in book.get_open_orders()
