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
        mock_config_manager.get_buy_ratio.return_value = 1.0
        mock_config_manager.get_sell_ratio.return_value = 1.0
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

    def test_simple_grid_home_states(self, grid_manager):
        grid_manager.initialize_grids_and_levels()

        for price, grid_level in grid_manager.grid_levels.items():
            expected = (
                GridCycleState.READY_TO_BUY if price <= grid_manager.central_price else GridCycleState.READY_TO_SELL
            )
            assert grid_level.home_state == expected

    def test_hedged_grid_home_states(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        for price, grid_level in grid_manager.grid_levels.items():
            expected = (
                GridCycleState.READY_TO_SELL
                if price == grid_manager.price_grids[-1]
                else GridCycleState.READY_TO_BUY_OR_SELL
            )
            assert grid_level.home_state == expected

    def test_get_trigger_price(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert grid_manager.get_trigger_price() == grid_manager.central_price

    def test_is_within_grid_range_inside(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert grid_manager.is_within_grid_range(1500) is True

    def test_is_within_grid_range_at_bounds(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert grid_manager.is_within_grid_range(1000) is True
        assert grid_manager.is_within_grid_range(2000) is True

    def test_is_within_grid_range_below_bottom(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert grid_manager.is_within_grid_range(999.99) is False

    def test_is_within_grid_range_above_top(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        assert grid_manager.is_within_grid_range(2000.01) is False

    def test_get_order_size_for_grid_level(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        current_price = 2000
        total_balance = 10000
        expected_order_size = total_balance / len(grid_manager.grid_levels) / current_price
        result = grid_manager.get_order_size_for_grid_level(total_balance, current_price, OrderSide.BUY)
        assert result == expected_order_size

    def test_get_order_size_with_buy_ratio(self, config_manager):
        config_manager.get_buy_ratio.return_value = 0.8
        grid_manager = GridManager(config_manager, StrategyType.SIMPLE_GRID)
        grid_manager.initialize_grids_and_levels()
        current_price = 2000
        total_balance = 10000
        base_size = total_balance / len(grid_manager.grid_levels) / current_price
        result = grid_manager.get_order_size_for_grid_level(total_balance, current_price, OrderSide.BUY)
        assert result == pytest.approx(base_size * 0.8)

    def test_get_order_size_with_sell_ratio(self, config_manager):
        config_manager.get_sell_ratio.return_value = 0.5
        grid_manager = GridManager(config_manager, StrategyType.SIMPLE_GRID)
        grid_manager.initialize_grids_and_levels()
        current_price = 2000
        total_balance = 10000
        base_size = total_balance / len(grid_manager.grid_levels) / current_price
        result = grid_manager.get_order_size_for_grid_level(total_balance, current_price, OrderSide.SELL)
        assert result == pytest.approx(base_size * 0.5)

    def test_get_order_size_default_ratios_are_symmetric(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        total_balance = 10000
        current_price = 2000
        buy_size = grid_manager.get_order_size_for_grid_level(total_balance, current_price, OrderSide.BUY)
        sell_size = grid_manager.get_order_size_for_grid_level(total_balance, current_price, OrderSide.SELL)
        assert buy_size == sell_size

    @pytest.mark.parametrize(
        ("price", "fiat", "crypto", "sell_ratio", "expected_quantity"),
        [
            # the 5 sell levels (1555.6 to 2000) all sit above 1500: half the portfolio goes to crypto
            (1500, 10000, 0.0, 1.0, 5000 / 1500),
            # 1777.8, 1888.9 and 2000 sell above 1700
            (1700, 10000, 0.0, 1.0, 3000 / 1700),
            # only the 2000 level sells above 1950
            (1950, 10000, 0.0, 1.0, 1000 / 1950),
            # below the center every sell level is above the price
            (1050, 10000, 0.0, 1.0, 5000 / 1050),
            # half-size sell orders need half the inventory
            (1500, 10000, 0.0, 0.5, 2500 / 1500),
            # held crypto counts toward the target: 4875 - 750
            (1500, 9000, 0.5, 1.0, 4125 / 1500),
            # held crypto (3900) already exceeds the 890 target
            (1950, 5000, 2.0, 1.0, 0.0),
            # nothing sells above the top level
            (2000, 10000, 0.0, 1.0, 0.0),
        ],
    )
    def test_get_initial_order_quantity_covers_sell_orders_above_price(
        self, config_manager, price, fiat, crypto, sell_ratio, expected_quantity
    ):
        config_manager.get_sell_ratio.return_value = sell_ratio
        grid_manager = GridManager(config_manager, StrategyType.SIMPLE_GRID)
        grid_manager.initialize_grids_and_levels()

        result = grid_manager.get_initial_order_quantity(fiat, crypto, price)

        assert result == pytest.approx(expected_quantity)

    @pytest.mark.parametrize(
        ("price", "expected_quantity"),
        [
            # 1555.6 is nearest and stays free: 1666.7 to 2000 sell
            (1520, 4000 / 1520),
            # 1444.4 is nearest and stays free: 1555.6 to 2000 sell
            (1460, 5000 / 1460),
            # 2000 is nearest and stays free: nothing sells
            (1950, 0.0),
            # armed exactly on the top level: nothing sells
            (2000, 0.0),
        ],
    )
    def test_get_initial_order_quantity_hedged_grid_funds_the_planned_sells(
        self, config_manager, price, expected_quantity
    ):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        result = grid_manager.get_initial_order_quantity(10000, 0.0, price)

        assert result == pytest.approx(expected_quantity)

    def test_hedged_grid_armed_on_top_level_plans_no_sell(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        plan = grid_manager.get_initial_order_plan(2000)

        assert all(side == OrderSide.BUY for _, side in plan)

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

    def test_get_sell_target_simple_grid_picks_lowest_free_sell_level(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        buy_grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]

        target = grid_manager.get_sell_target(buy_grid_level)

        assert target is grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        assert target.state == GridCycleState.READY_TO_SELL

    def test_get_sell_target_hedged_grid_is_the_level_above(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        buy_grid_level = grid_manager.grid_levels[grid_manager._sorted_prices[2]]

        target = grid_manager.get_sell_target(buy_grid_level)

        assert target is grid_manager.grid_levels[grid_manager._sorted_prices[3]]

    def test_get_buy_target_hedged_grid_is_the_level_below(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        sell_grid_level = grid_manager.grid_levels[grid_manager._sorted_prices[3]]

        target = grid_manager.get_buy_target(sell_grid_level)

        assert target is grid_manager.grid_levels[grid_manager._sorted_prices[2]]

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

    def test_complete_order_simple_grid_buy_frees_the_level(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL

        grid_manager.complete_order(grid_level, OrderSide.BUY, has_open_orders=False)

        assert grid_level.state == GridCycleState.READY_TO_BUY

    def test_complete_order_simple_grid_sell_keeps_waiting_while_stacked(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL

        grid_manager.complete_order(grid_level, OrderSide.SELL, has_open_orders=True)

        assert grid_level.state == GridCycleState.WAITING_FOR_SELL_FILL

    def test_complete_order_simple_grid_sell_frees_the_level(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL

        grid_manager.complete_order(grid_level, OrderSide.SELL, has_open_orders=False)

        assert grid_level.state == GridCycleState.READY_TO_SELL

    def test_complete_order_hedged_grid(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL

        grid_manager.complete_order(grid_level, OrderSide.BUY, has_open_orders=False)

        assert grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL

    def test_complete_order_hedged_grid_buy_leaves_the_paired_level_alone(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        buy_level = grid_manager.grid_levels[grid_manager._sorted_prices[2]]
        sell_level = grid_manager.grid_levels[grid_manager._sorted_prices[3]]
        grid_manager.pair_grid_levels(buy_level, sell_level, "sell")
        buy_level.state = GridCycleState.WAITING_FOR_BUY_FILL
        sell_level.state = GridCycleState.WAITING_FOR_BUY_FILL  # holds another order

        grid_manager.complete_order(buy_level, OrderSide.BUY, has_open_orders=False)

        assert buy_level.state == GridCycleState.READY_TO_BUY_OR_SELL
        assert sell_level.state == GridCycleState.WAITING_FOR_BUY_FILL

    def test_complete_order_hedged_grid_sell_leaves_the_paired_level_alone(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        buy_level = grid_manager.grid_levels[grid_manager._sorted_prices[2]]
        sell_level = grid_manager.grid_levels[grid_manager._sorted_prices[3]]
        grid_manager.pair_grid_levels(sell_level, buy_level, "buy")
        sell_level.state = GridCycleState.WAITING_FOR_SELL_FILL
        buy_level.state = GridCycleState.WAITING_FOR_SELL_FILL  # holds another order

        grid_manager.complete_order(sell_level, OrderSide.SELL, has_open_orders=False)

        assert sell_level.state == GridCycleState.READY_TO_BUY_OR_SELL
        assert buy_level.state == GridCycleState.WAITING_FOR_SELL_FILL

    def test_complete_order_hedged_top_level_returns_to_sell_only(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        top_level = grid_manager.grid_levels[grid_manager._sorted_prices[-1]]
        top_level.state = GridCycleState.WAITING_FOR_SELL_FILL

        grid_manager.complete_order(top_level, OrderSide.SELL, has_open_orders=False)

        assert top_level.state == GridCycleState.READY_TO_SELL
        assert grid_manager.can_place_order(top_level, OrderSide.BUY) is False

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

    def test_rollback_order_placement_simple_grid_buy(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL

        grid_manager.rollback_order_placement(grid_level, OrderSide.BUY, has_open_orders=False)

        assert grid_level.state == GridCycleState.READY_TO_BUY

    def test_rollback_order_placement_simple_grid_sell(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL

        grid_manager.rollback_order_placement(grid_level, OrderSide.SELL, has_open_orders=False)

        assert grid_level.state == GridCycleState.READY_TO_SELL

    def test_rollback_order_placement_hedged_grid_allows_both_sides(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()

        grid_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL
        grid_manager.rollback_order_placement(grid_level, OrderSide.BUY, has_open_orders=False)
        assert grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL

        grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL
        grid_manager.rollback_order_placement(grid_level, OrderSide.SELL, has_open_orders=False)
        assert grid_level.state == GridCycleState.READY_TO_BUY_OR_SELL

    def test_rollback_order_placement_keeps_waiting_while_other_orders_remain(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]
        grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL

        grid_manager.rollback_order_placement(grid_level, OrderSide.SELL, has_open_orders=True)

        assert grid_level.state == GridCycleState.WAITING_FOR_SELL_FILL

    def test_get_buy_target_simple_grid_picks_highest_free_buy_level(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        sell_grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]

        target = grid_manager.get_buy_target(sell_grid_level)

        assert target is grid_manager.grid_levels[grid_manager.sorted_buy_grids[-1]]

    def test_get_buy_target_simple_grid_ignores_pairing(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        sell_grid_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[1]]
        sell_grid_level.paired_buy_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[0]]

        target = grid_manager.get_buy_target(sell_grid_level)

        assert target is grid_manager.grid_levels[grid_manager.sorted_buy_grids[-1]]

    def test_get_buy_target_simple_grid_skips_level_with_open_buy(self, grid_manager):
        grid_manager.initialize_grids_and_levels()
        highest_buy_level = grid_manager.grid_levels[grid_manager.sorted_buy_grids[-1]]
        grid_manager.mark_order_pending(highest_buy_level, Mock(spec=Order, side=OrderSide.BUY))
        lowest_sell_level = grid_manager.grid_levels[grid_manager.sorted_sell_grids[0]]

        target = grid_manager.get_buy_target(lowest_sell_level)

        assert target is grid_manager.grid_levels[grid_manager.sorted_buy_grids[-2]]

    def test_get_buy_target_hedged_grid_from_bottom_level_is_none(self, config_manager):
        grid_manager = GridManager(config_manager, StrategyType.HEDGED_GRID)
        grid_manager.initialize_grids_and_levels()
        bottom_level = grid_manager.grid_levels[grid_manager._sorted_prices[0]]

        assert grid_manager.get_buy_target(bottom_level) is None

    def test_get_initial_order_plan_uses_the_current_layout(self, grid_manager):
        grid_manager.initialize_grids_and_levels()

        plan = grid_manager.get_initial_order_plan(1500)

        assert [level.price for level, side in plan if side == OrderSide.BUY] == [
            p for p in grid_manager.sorted_buy_grids if p < 1500
        ]
        assert [level.price for level, side in plan if side == OrderSide.SELL] == [
            p for p in grid_manager.sorted_sell_grids if p > 1500
        ]

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


@pytest.fixture
def config_manager_simple():
    mock_config_manager = Mock(spec=ConfigManager)
    mock_config_manager.get_num_grids.return_value = 5
    mock_config_manager.get_atr_spacing_multiplier.return_value = 1.0
    mock_config_manager.get_buy_ratio.return_value = 1.0
    mock_config_manager.get_sell_ratio.return_value = 1.0
    return mock_config_manager


@pytest.fixture
def grid_manager_simple(config_manager_simple):
    return GridManager(config_manager_simple, StrategyType.SIMPLE_GRID)


class TestRegrid:
    def test_regrid_geometry(self, grid_manager_simple):
        gm = grid_manager_simple  # num_grids=5, atr_spacing_multiplier=1.0
        gm.regrid(center_price=100.0, atr=2.0)

        # spacing = 1.0 * 2.0 = 2.0 ; half span = (5-1)/2 * 2 = 4
        assert gm.price_grids == pytest.approx([96.0, 98.0, 100.0, 102.0, 104.0])
        assert gm.central_price == pytest.approx(100.0)
        assert gm.atr_grid == pytest.approx(2.0)
        assert gm.is_initialized is True

    def test_regrid_rebuilds_levels_and_lookup_tables(self, grid_manager_simple):
        gm = grid_manager_simple
        gm.regrid(center_price=100.0, atr=2.0)
        assert set(gm.grid_levels.keys()) == set(gm.price_grids)
        assert gm._sorted_prices == sorted(gm.price_grids)
        # simple grid: levels below center buy, above center sell
        assert 96.0 in gm.sorted_buy_grids
        assert 104.0 in gm.sorted_sell_grids

    def test_regrid_rejects_non_positive_atr(self, grid_manager_simple):
        with pytest.raises(ValueError, match="non-positive or NaN ATR"):
            grid_manager_simple.regrid(center_price=100.0, atr=0.0)

    def test_regrid_rejects_nan_atr(self, grid_manager_simple):
        with pytest.raises(ValueError, match="non-positive or NaN ATR"):
            grid_manager_simple.regrid(center_price=100.0, atr=float("nan"))

    def test_regrid_rejects_bottom_below_zero(self, grid_manager_simple):
        # center 1.0, atr 1.0 => bottom = 1.0 - 2*1.0 = -1.0
        with pytest.raises(ValueError, match="non-positive bottom price"):
            grid_manager_simple.regrid(center_price=1.0, atr=1.0)

    def test_not_initialized_before_any_init(self, grid_manager_simple):
        assert grid_manager_simple.is_initialized is False

    def test_hedged_plan_after_regrid_leaves_the_level_nearest_the_new_centre_free(self, config_manager_simple):
        gm = GridManager(config_manager_simple, StrategyType.HEDGED_GRID)
        gm.regrid(center_price=100.0, atr=2.0)  # levels 96, 98, 100, 102, 104

        plan = gm.get_initial_order_plan(100.0)
        shifted_plan = gm.get_initial_order_plan(101.5)

        assert [(level.price, side) for level, side in plan] == [
            (pytest.approx(96.0), OrderSide.BUY),
            (pytest.approx(98.0), OrderSide.BUY),
            (pytest.approx(102.0), OrderSide.SELL),
            (pytest.approx(104.0), OrderSide.SELL),
        ]
        assert [(level.price, side) for level, side in shifted_plan] == [
            (pytest.approx(96.0), OrderSide.BUY),
            (pytest.approx(98.0), OrderSide.BUY),
            (pytest.approx(100.0), OrderSide.BUY),
            (pytest.approx(104.0), OrderSide.SELL),
        ]


class TestApplyGeometry:
    def test_apply_geometry_rebuilds_levels_and_lookup_tables(self, grid_manager_simple):
        gm = grid_manager_simple
        price_grids = [90.0, 95.0, 100.0, 105.0, 110.0]

        gm.apply_geometry(price_grids, central_price=100.0, atr_grid=4.2)

        assert gm.price_grids == price_grids
        assert gm.central_price == pytest.approx(100.0)
        assert gm.atr_grid == pytest.approx(4.2)
        assert gm.is_initialized is True
        assert set(gm.grid_levels.keys()) == set(price_grids)
        assert gm._sorted_prices == sorted(price_grids)
        assert gm._price_index_map == {p: i for i, p in enumerate(sorted(price_grids))}

    def test_apply_geometry_accepts_none_atr_grid(self, grid_manager_simple):
        gm = grid_manager_simple
        gm.apply_geometry([90.0, 100.0, 110.0], central_price=100.0, atr_grid=None)
        assert gm.atr_grid is None
        assert gm.is_initialized is True

    def test_apply_geometry_rejects_empty_grid(self, grid_manager_simple):
        with pytest.raises(ValueError, match="empty grid geometry"):
            grid_manager_simple.apply_geometry([], central_price=100.0, atr_grid=1.0)

    def test_restored_level_returns_to_its_home_state(self, config_manager_simple):
        gm = GridManager(config_manager_simple, StrategyType.HEDGED_GRID)
        gm.apply_geometry([90.0, 100.0, 110.0], central_price=100.0, atr_grid=None)
        top_level = gm.grid_levels[110.0]
        top_level.state = GridCycleState.WAITING_FOR_SELL_FILL  # as restored from the database

        gm.complete_order(top_level, OrderSide.SELL, has_open_orders=False)

        assert top_level.state == GridCycleState.READY_TO_SELL
