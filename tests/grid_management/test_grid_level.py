from unittest.mock import Mock

import pytest

from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.order_handling.order import Order


class TestGridLevel:
    @pytest.fixture
    def grid_level(self):
        return GridLevel(price=1000, state=GridCycleState.READY_TO_BUY)

    def test_grid_level_initialization(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.READY_TO_BUY)

        assert grid_level.price == 1000
        assert grid_level.state == GridCycleState.READY_TO_BUY
        assert grid_level.orders == []
        assert grid_level.paired_buy_level is None
        assert grid_level.paired_sell_level is None

    def test_add_order(self, grid_level):
        mock_order = Mock(spec=Order)
        grid_level.add_order(mock_order)

        assert len(grid_level.orders) == 1
        assert grid_level.orders[0] == mock_order

    def test_str_representation(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.READY_TO_BUY)
        grid_level.paired_buy_level = GridLevel(price=900, state=GridCycleState.READY_TO_SELL)
        grid_level.paired_sell_level = GridLevel(price=1100, state=GridCycleState.READY_TO_BUY)

        repr_str = str(grid_level)
        assert "price=1000" in repr_str
        assert "state=READY_TO_BUY" in repr_str
        assert "paired_buy_level=900" in repr_str
        assert "paired_sell_level=1100" in repr_str

    def test_update_paired_levels(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.READY_TO_BUY)
        paired_buy_level = GridLevel(price=900, state=GridCycleState.READY_TO_SELL)
        paired_sell_level = GridLevel(price=1100, state=GridCycleState.READY_TO_BUY)

        grid_level.paired_buy_level = paired_buy_level
        grid_level.paired_sell_level = paired_sell_level

        assert grid_level.paired_buy_level.price == 900
        assert grid_level.paired_sell_level.price == 1100

    def test_state_transition_to_waiting_for_buy_fill(self, grid_level):
        grid_level.state = GridCycleState.READY_TO_BUY
        mock_order = Mock(spec=Order)
        grid_level.add_order(mock_order)

        grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL
        assert grid_level.state == GridCycleState.WAITING_FOR_BUY_FILL

    def test_state_transition_to_ready_to_sell(self, grid_level):
        grid_level.state = GridCycleState.READY_TO_BUY
        mock_order = Mock(spec=Order)
        grid_level.add_order(mock_order)

        grid_level.state = GridCycleState.READY_TO_SELL
        assert grid_level.state == GridCycleState.READY_TO_SELL

    def test_state_transition_to_waiting_for_sell_fill(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.READY_TO_SELL)
        mock_order = Mock(spec=Order)
        grid_level.add_order(mock_order)

        grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL
        assert grid_level.state == GridCycleState.WAITING_FOR_SELL_FILL

    def test_state_transition_to_ready_to_buy_or_sell(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.WAITING_FOR_SELL_FILL)

        grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
        assert grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL

    def test_paired_levels_initialization(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.READY_TO_BUY)
        paired_buy_level = GridLevel(price=900, state=GridCycleState.READY_TO_SELL)
        paired_sell_level = GridLevel(price=1100, state=GridCycleState.READY_TO_BUY)

        grid_level.paired_buy_level = paired_buy_level
        grid_level.paired_sell_level = paired_sell_level

        assert grid_level.paired_buy_level == paired_buy_level
        assert grid_level.paired_sell_level == paired_sell_level

    def test_orders_list(self):
        grid_level = GridLevel(price=1000, state=GridCycleState.READY_TO_BUY)
        order1 = Mock(spec=Order)
        order2 = Mock(spec=Order)

        grid_level.add_order(order1)
        grid_level.add_order(order2)

        assert len(grid_level.orders) == 2
        assert grid_level.orders[0] == order1
        assert grid_level.orders[1] == order2
