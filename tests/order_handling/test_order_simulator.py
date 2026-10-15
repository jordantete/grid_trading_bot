from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator


class TestOrderSimulator:
    @pytest.fixture
    def setup_order_simulator(self):
        order_book = Mock()
        grid_manager = Mock()
        event_bus = Mock(spec=EventBus)
        event_bus.publish = AsyncMock()

        simulator = OrderSimulator(
            order_book=order_book,
            grid_manager=grid_manager,
            event_bus=event_bus,
        )
        return simulator, order_book, grid_manager, event_bus

    @pytest.mark.asyncio
    async def test_simulate_order_fills_partial_fill(self, setup_order_simulator):
        simulator, order_book, grid_manager, _ = setup_order_simulator
        mock_order = Mock(
            side=OrderSide.BUY,
            price=48000,
            amount=0.02,
            filled=0.01,
            remaining=0.01,
            status=OrderStatus.OPEN,
        )
        order_book.get_open_orders.return_value = [mock_order]
        grid_manager.sorted_buy_grids = [48000]
        grid_manager.sorted_sell_grids = []

        await simulator.simulate_order_fills(49000, 47000, 1234567890)

        assert mock_order.filled == 0.02
        assert mock_order.remaining == 0.0
        assert mock_order.status == OrderStatus.CLOSED

    @pytest.mark.asyncio
    async def test_simulate_fill(self, setup_order_simulator):
        simulator, _, _, event_bus = setup_order_simulator
        mock_order = Mock(
            amount=1.0,
            side=OrderSide.BUY,
            price=50000,
        )
        timestamp = 1234567890

        await simulator.simulate_fill(mock_order, timestamp)

        assert mock_order.filled == mock_order.amount
        assert mock_order.remaining == 0.0
        assert mock_order.status == OrderStatus.CLOSED
        assert mock_order.last_trade_timestamp == timestamp
        event_bus.publish.assert_awaited_with(Events.ORDER_FILLED, mock_order)

    @pytest.mark.asyncio
    async def test_simulate_fill_buy_with_slippage(self):
        order_book = Mock()
        grid_manager = Mock()
        event_bus = Mock(spec=EventBus)
        event_bus.publish = AsyncMock()

        simulator = OrderSimulator(
            order_book=order_book,
            grid_manager=grid_manager,
            event_bus=event_bus,
            slippage=0.001,
        )
        mock_order = Mock(
            amount=1.0,
            side=OrderSide.BUY,
            price=50000.0,
        )

        await simulator.simulate_fill(mock_order, 1234567890)

        assert mock_order.average == 50000.0 * 1.001
        assert mock_order.filled == mock_order.amount
        assert mock_order.status == OrderStatus.CLOSED

    @pytest.mark.asyncio
    async def test_simulate_fill_sell_with_slippage(self):
        order_book = Mock()
        grid_manager = Mock()
        event_bus = Mock(spec=EventBus)
        event_bus.publish = AsyncMock()

        simulator = OrderSimulator(
            order_book=order_book,
            grid_manager=grid_manager,
            event_bus=event_bus,
            slippage=0.001,
        )
        mock_order = Mock(
            amount=1.0,
            side=OrderSide.SELL,
            price=50000.0,
        )

        await simulator.simulate_fill(mock_order, 1234567890)

        assert mock_order.average == 50000.0 * 0.999
        assert mock_order.filled == mock_order.amount
        assert mock_order.status == OrderStatus.CLOSED

    @pytest.mark.asyncio
    async def test_simulate_fill_no_slippage_by_default(self, setup_order_simulator):
        simulator, _, _, _ = setup_order_simulator
        mock_order = Mock(
            amount=1.0,
            side=OrderSide.BUY,
            price=50000.0,
            average=50000.0,
        )

        await simulator.simulate_fill(mock_order, 1234567890)

        # average should not be reassigned when slippage is 0
        assert mock_order.average == 50000.0


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
        event_bus = Mock(spec=EventBus)
        event_bus.publish = AsyncMock()
        simulator = OrderSimulator(order_book=order_book, grid_manager=grid_manager, event_bus=event_bus)

        await simulator.simulate_order_fills(high, low, 1234567890)

        assert all(call.args[0] == Events.ORDER_FILLED for call in event_bus.publish.call_args_list)
        return [call.args[1].identifier for call in event_bus.publish.call_args_list]

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
