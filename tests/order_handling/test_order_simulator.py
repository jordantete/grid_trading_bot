from unittest.mock import AsyncMock, Mock

import pytest

from core.bot_management.event_bus import EventBus, Events
from core.order_handling.order import OrderSide, OrderStatus
from core.order_handling.order_simulator import OrderSimulator


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

        await simulator._simulate_fill(mock_order, timestamp)

        assert mock_order.filled == mock_order.amount
        assert mock_order.remaining == 0.0
        assert mock_order.status == OrderStatus.CLOSED
        assert mock_order.last_trade_timestamp == timestamp
        event_bus.publish.assert_awaited_with(Events.ORDER_FILLED, mock_order)
