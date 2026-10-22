from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator


def _limit_order(identifier: str, side: OrderSide, price: float) -> Order:
    return TestOrderSimulatorFillSequence._order(identifier, side, price)


class TestOrderSimulator:
    @pytest.fixture
    def setup_order_simulator(self):
        order_book = Mock()
        grid_manager = Mock()
        handler = Mock()
        handler.apply_order_update = AsyncMock()
        simulator = OrderSimulator(order_book=order_book, grid_manager=grid_manager, order_update_handler=handler)
        return simulator, order_book, grid_manager, handler

    @pytest.mark.asyncio
    async def test_a_crossed_order_is_handed_over_filled_in_full(self, setup_order_simulator):
        simulator, order_book, grid_manager, handler = setup_order_simulator
        order = _limit_order("b48", OrderSide.BUY, 48000.0)
        order_book.get_open_orders.return_value = [order]
        grid_manager.sorted_buy_grids = [48000.0]
        grid_manager.sorted_sell_grids = []

        await simulator.simulate_order_fills(49000, 47000, 1234567890)

        fill = handler.apply_order_update.await_args.args[0]
        assert fill is not order
        assert (fill.identifier, fill.status, fill.filled, fill.remaining) == ("b48", OrderStatus.CLOSED, 1.0, 0.0)
        assert (fill.timestamp, fill.last_trade_timestamp) == (1234567890, 1234567890)
        assert order.status == OrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_a_candle_time_is_stored_in_milliseconds(self, setup_order_simulator):
        simulator, order_book, grid_manager, handler = setup_order_simulator
        order_book.get_open_orders.return_value = [_limit_order("b48", OrderSide.BUY, 48000.0)]
        grid_manager.sorted_buy_grids = [48000.0]
        grid_manager.sorted_sell_grids = []

        await simulator.simulate_order_fills(49000, 47000, pd.Timestamp("2024-08-01 00:01:00"))

        fill = handler.apply_order_update.await_args.args[0]
        assert (fill.timestamp, fill.last_trade_timestamp) == (1722470460000, 1722470460000)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("side", "factor"), [(OrderSide.BUY, 1.001), (OrderSide.SELL, 0.999)])
    async def test_slippage_moves_the_fill_price_against_the_order(self, side, factor):
        handler = Mock()
        handler.apply_order_update = AsyncMock()
        simulator = OrderSimulator(order_book=Mock(), grid_manager=Mock(), order_update_handler=handler, slippage=0.001)

        await simulator.simulate_fill(_limit_order("o1", side, 50000.0), 1234567890)

        assert handler.apply_order_update.await_args.args[0].average == pytest.approx(50000.0 * factor)

    @pytest.mark.asyncio
    async def test_no_slippage_keeps_the_average(self, setup_order_simulator):
        simulator, _, _, handler = setup_order_simulator
        order = _limit_order("o1", OrderSide.BUY, 50000.0)
        order.average = 50000.0

        await simulator.simulate_fill(order, 1234567890)

        assert handler.apply_order_update.await_args.args[0].average == 50000.0


class TestOrderSimulatorFillSequence:
    @staticmethod
    def _order(identifier: str, side: OrderSide, price: float) -> Order:
        return Order(
            identifier=identifier,
            status=OrderStatus.OPEN,
            order_type=OrderType.LIMIT,
            side=side,
            price=price,
            average=None,
            amount=1.0,
            filled=0.0,
            remaining=1.0,
            timestamp=0,
            datetime=None,
            last_trade_timestamp=None,
            symbol="X/USDT",
            time_in_force="GTC",
        )

    @staticmethod
    async def _published_ids(orders: list[Order], buy_grids: list[float], sell_grids: list[float], high, low):
        order_book = OrderBook()
        for order in orders:
            order_book.add_order(order, Mock())
        grid_manager = Mock(sorted_buy_grids=buy_grids, sorted_sell_grids=sell_grids)
        handler = Mock()
        handler.apply_order_update = AsyncMock()
        simulator = OrderSimulator(order_book=order_book, grid_manager=grid_manager, order_update_handler=handler)

        await simulator.simulate_order_fills(high, low, 1234567890)

        return [call.args[0].identifier for call in handler.apply_order_update.await_args_list]

    @pytest.mark.asyncio
    async def test_buys_fill_from_highest_to_lowest(self):
        orders = [self._order("b110", OrderSide.BUY, 110.0), self._order("b120", OrderSide.BUY, 120.0)]

        ids = await self._published_ids(orders, [110.0, 120.0], [], high=125.0, low=105.0)

        assert ids == ["b120", "b110"]

    @pytest.mark.asyncio
    async def test_sells_fill_from_lowest_to_highest(self):
        orders = [self._order("s140", OrderSide.SELL, 140.0), self._order("s130", OrderSide.SELL, 130.0)]

        ids = await self._published_ids(orders, [], [130.0, 140.0], high=145.0, low=125.0)

        assert ids == ["s130", "s140"]

    @pytest.mark.asyncio
    async def test_buys_fill_before_sells(self):
        orders = [self._order("s130", OrderSide.SELL, 130.0), self._order("b110", OrderSide.BUY, 110.0)]

        ids = await self._published_ids(orders, [110.0], [130.0], high=135.0, low=105.0)

        assert ids == ["b110", "s130"]

    @pytest.mark.asyncio
    async def test_stacked_sells_keep_insertion_order(self):
        orders = [self._order("first", OrderSide.SELL, 130.0), self._order("second", OrderSide.SELL, 130.0)]

        ids = await self._published_ids(orders, [], [130.0], high=135.0, low=125.0)

        assert ids == ["first", "second"]
