from unittest.mock import Mock

import numpy as np
import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType
from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.order import Order, OrderSide


class TestGridManager:
    @pytest.fixture
    def config_manager(self):
        mock_config_manager = Mock(spec=ConfigManager)
        mock_config_manager.get_bottom_range.return_value = 1000
        mock_config_manager.get_top_range.return_value = 2000
        mock_config_manager.get_num_grids.return_value = 10
        mock_config_manager.get_spacing_type.return_value = SpacingType.ARITHMETIC
        return mock_config_manager

    @pytest.fixture
    def grid_manager(self, config_manager):
        return GridManager(config_manager, StrategyType.SIMPLE_GRID)

    def test_initialize_grids_and_levels_simple_grid(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert len(grid_manager.grid_levels) == len(grid_manager.price_grids)

        for price, grid_level in grid_manager.grid_levels.items():
            assert isinstance(grid_level, GridLevel)
            if price <= grid_manager.central_price:
                assert grid_level.state == GridCycleState.READY_TO_BUY
            else:
                assert grid_level.state == GridCycleState.READY_TO_SELL

    def test_initialize_grids_and_levels_hedged_grid(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)

        grid_manager.initialize_grids_and_levels()
        assert len(grid_manager.grid_levels) == len(grid_manager.price_grids)

        for price, grid_level in grid_manager.grid_levels.items():
            assert isinstance(grid_level, GridLevel)
            if price == grid_manager.price_grids[-1]:
                assert grid_level.state == GridCycleState.READY_TO_SELL
            else:
                assert grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL

    def test_get_trigger_price(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert grid_manager.get_trigger_price() == grid_manager.central_price

    def test_get_order_size_for_grid_level(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        current_price = 2000
        total_balance = 10000  # Mocked initial balance
        expected_order_size = total_balance / len(grid_manager.grid_levels) / current_price
        result = grid_manager.get_order_size_for_grid_level(total_balance, current_price)
        assert result == expected_order_size

    def test_get_initial_order_quantity(self, grid_manager):
        current_fiat_balance = 5000  # Half of the total balance
        current_crypto_balance = 0.5
        current_price = 2000
        expected_quantity = (
            (current_fiat_balance + (current_crypto_balance * current_price)) / 2
            - (current_crypto_balance * current_price)
        ) / current_price
        result = grid_manager.get_initial_order_quantity(current_fiat_balance, current_crypto_balance, current_price)
        assert result == expected_quantity

    def test_pair_grid_levels(self, grid_manager):
        source_grid_level = Mock(spec=GridLevel, price=1000)
        target_grid_level = Mock(spec=GridLevel, price=1100)
        grid_manager.pair_grid_levels(source_grid_level, target_grid_level, pairing_type="buy")
        assert source_grid_level.paired_buy_level == target_grid_level
        assert target_grid_level.paired_sell_level == source_grid_level

    def test_pair_grid_levels_invalid_type(self, grid_manager):
        source_grid_level = Mock(spec=GridLevel, price=1000)
        target_grid_level = Mock(spec=GridLevel, price=1100)

        with pytest.raises(ValueError, match="Invalid pairing type"):
            grid_manager.pair_grid_levels(source_grid_level, target_grid_level, pairing_type="invalid")

    def test_get_paired_sell_level_simple_grid(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        paired_sell_level = grid_manager.get_paired_sell_level(buy_grid_level)
        assert paired_sell_level.price > buy_grid_level.price
        assert paired_sell_level.state == GridCycleState.READY_TO_SELL

    def test_get_paired_sell_level_hedged_grid(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        paired_sell_level = grid_manager.get_paired_sell_level(buy_grid_level)

        assert paired_sell_level is not None
        assert paired_sell_level.price > buy_grid_level.price
        assert paired_sell_level.state in {GridCycleState.READY_TO_SELL, GridCycleState.READY_TO_BUY_OR_SELL}

    def test_get_grid_level_below(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[1]]
        lower_level = grid_manager.get_grid_level_below(grid_level)
        assert lower_level.price < grid_level.price

    def test_mark_order_pending_after_buy(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        order = Mock(spec=Order, side=OrderSide.BUY)

        grid_manager.mark_order_pending(grid_level, order)
        assert grid_level.state == GridCycleState.WAITING_FOR_BUY_FILL
        assert order in grid_level.orders

    def test_mark_order_pending_after_sell(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        order = Mock(spec=Order, side=OrderSide.SELL)

        grid_manager.mark_order_pending(grid_level, order)

        assert grid_level.state == GridCycleState.WAITING_FOR_SELL_FILL
        assert order in grid_level.orders

    def test_complete_order_simple_grid(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]

        grid_manager.complete_order(grid_level, OrderSide.BUY)
        assert grid_level.state == GridCycleState.READY_TO_SELL

    def test_complete_order_hedged_grid(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        grid_manager.complete_order(grid_level, OrderSide.BUY)
        assert grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL

    def test_complete_order_simple_grid_buy(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]

        grid_manager.complete_order(grid_level, OrderSide.BUY)

        # Validate the state transition to READY_TO_SELL
        assert grid_level.state == GridCycleState.READY_TO_SELL

    def test_complete_order_simple_grid_sell(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]

        grid_manager.complete_order(grid_level, OrderSide.SELL)

        assert grid_level.state == GridCycleState.READY_TO_BUY

    def test_complete_order_hedged_grid_buy(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        sell_grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]

        # Pair levels for testing
        grid_manager.pair_grid_levels(sell_grid_level, buy_grid_level, "buy")

        grid_manager.complete_order(buy_grid_level, OrderSide.BUY)

        # Validate transitions
        assert buy_grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL
        assert sell_grid_level.state == GridCycleState.READY_TO_SELL

    def test_complete_order_hedged_grid_sell(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        sell_grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]

        # Pair levels for testing
        grid_manager.pair_grid_levels(buy_grid_level, sell_grid_level, "sell")

        grid_manager.complete_order(sell_grid_level, OrderSide.SELL)

        assert sell_grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL
        assert buy_grid_level.state == GridCycleState.READY_TO_BUY

    def test_can_place_order_simple_grid(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        sell_grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]

        assert grid_manager.can_place_order(buy_grid_level, OrderSide.BUY) is True
        assert grid_manager.can_place_order(sell_grid_level, OrderSide.SELL) is True

    def test_can_place_order_hedged_grid(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        sell_grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]

        assert grid_manager.can_place_order(buy_grid_level, OrderSide.BUY) is True
        assert grid_manager.can_place_order(sell_grid_level, OrderSide.SELL) is True

    def test_calculate_price_grids_and_central_price_arithmetic(self, grid_manager):
        expected_grids = np.linspace(1000, 2000, 10)
        grids, central_price = grid_manager._calculate_price_grids_and_central_price()
        np.testing.assert_array_almost_equal(grids, expected_grids, decimal=10)
        assert central_price == 1500

    def test_calculate_price_grids_and_central_price_geometric(self, config_manager):
        config_manager.get_spacing_type.return_value = SpacingType.GEOMETRIC
        config_manager.get_top_range.return_value = 2000
        config_manager.get_bottom_range.return_value = 1000
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)

        expected_grids = [
            1000,
            1080.059738892306,
            1166.5290395761165,
            1259.921049894873,
            1360.7900001743767,
            1469.7344922755985,
            1587.401051968199,
            1714.4879657061451,
            1851.7494245745802,
            2000,
        ]
        grids, central_price = grid_manager._calculate_price_grids_and_central_price()
        np.testing.assert_array_almost_equal(grids, expected_grids, decimal=5)
        assert central_price == 1415.2622462249876
