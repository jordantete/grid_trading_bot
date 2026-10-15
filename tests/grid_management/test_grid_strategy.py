import logging

import pytest

from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.grid_management.grid_strategy import GridLayout, HedgedGridStrategy, SimpleGridStrategy
from grid_trading_bot.core.order_handling.order import OrderSide

PRICES = [100.0, 110.0, 120.0, 130.0, 140.0]
LOGGER = logging.getLogger("GridManager")


def _layout(strategy, prices=PRICES, central_price=120.0):
    buy_grids, sell_grids, grid_levels = strategy.initialize_levels(prices, central_price)
    sorted_prices = sorted(prices)
    return GridLayout(
        sorted_prices=sorted_prices,
        price_index_map={price: index for index, price in enumerate(sorted_prices)},
        grid_levels=grid_levels,
        sorted_buy_grids=buy_grids,
        sorted_sell_grids=sell_grids,
    )


def _prices(plan):
    return [(level.price, side) for level, side in plan]


class TestGridLayout:
    def test_neighbour_inside_the_grid(self):
        layout = _layout(HedgedGridStrategy())

        assert layout.neighbour(layout.grid_levels[120.0], 1).price == 130.0
        assert layout.neighbour(layout.grid_levels[120.0], -1).price == 110.0

    def test_neighbour_past_either_end_is_none(self):
        layout = _layout(HedgedGridStrategy())

        assert layout.neighbour(layout.grid_levels[140.0], 1) is None
        assert layout.neighbour(layout.grid_levels[100.0], -1) is None

    def test_neighbour_of_a_level_outside_the_grid_is_none(self):
        layout = _layout(HedgedGridStrategy())
        stale = GridLevel(125.0, GridCycleState.READY_TO_BUY_OR_SELL)

        assert layout.neighbour(stale, 1) is None
        assert layout.neighbour(stale, -1) is None


class TestHedgedInitialPlan:
    @pytest.mark.parametrize(
        ("current_price", "expected"),
        [
            # nearest level 110 stays free
            (
                112.0,
                [(100.0, OrderSide.BUY), (120.0, OrderSide.SELL), (130.0, OrderSide.SELL), (140.0, OrderSide.SELL)],
            ),
            # nearest level 120 stays free
            (118.0, [(100.0, OrderSide.BUY), (110.0, OrderSide.BUY), (130.0, OrderSide.SELL), (140.0, OrderSide.SELL)]),
            # tie between 110 and 120: the lower one stays free
            (
                115.0,
                [(100.0, OrderSide.BUY), (120.0, OrderSide.SELL), (130.0, OrderSide.SELL), (140.0, OrderSide.SELL)],
            ),
            # exactly on a level: that level stays free
            (120.0, [(100.0, OrderSide.BUY), (110.0, OrderSide.BUY), (130.0, OrderSide.SELL), (140.0, OrderSide.SELL)]),
            # on the bottom level: no buy
            (
                100.0,
                [(110.0, OrderSide.SELL), (120.0, OrderSide.SELL), (130.0, OrderSide.SELL), (140.0, OrderSide.SELL)],
            ),
            # on the top level: no sell
            (140.0, [(100.0, OrderSide.BUY), (110.0, OrderSide.BUY), (120.0, OrderSide.BUY), (130.0, OrderSide.BUY)]),
        ],
    )
    def test_plan_leaves_the_level_nearest_the_price_free(self, current_price, expected):
        strategy = HedgedGridStrategy()

        plan = strategy.initial_order_plan(_layout(strategy), current_price)

        assert _prices(plan) == expected


class TestHedgedTargets:
    def test_sell_target_is_the_free_level_above(self):
        strategy = HedgedGridStrategy()
        layout = _layout(strategy)

        assert strategy.sell_target(layout.grid_levels[110.0], layout, LOGGER) is layout.grid_levels[120.0]

    def test_sell_target_busy_is_refused_and_logged(self, caplog):
        strategy = HedgedGridStrategy()
        layout = _layout(strategy)
        layout.grid_levels[120.0].state = GridCycleState.WAITING_FOR_SELL_FILL

        with caplog.at_level(logging.ERROR):
            target = strategy.sell_target(layout.grid_levels[110.0], layout, LOGGER)

        assert target is None
        assert "is busy" in caplog.text

    def test_sell_target_of_a_level_outside_the_grid_is_refused_and_logged(self, caplog):
        strategy = HedgedGridStrategy()
        layout = _layout(strategy)
        stale = GridLevel(125.0, GridCycleState.READY_TO_BUY_OR_SELL)

        with caplog.at_level(logging.ERROR):
            target = strategy.sell_target(stale, layout, LOGGER)

        assert target is None
        assert "No grid level next to" in caplog.text

    def test_buy_target_is_the_free_level_below(self):
        strategy = HedgedGridStrategy()
        layout = _layout(strategy)

        assert strategy.buy_target(layout.grid_levels[130.0], layout, LOGGER) is layout.grid_levels[120.0]

    def test_buy_target_busy_is_refused_and_logged(self, caplog):
        strategy = HedgedGridStrategy()
        layout = _layout(strategy)
        layout.grid_levels[120.0].state = GridCycleState.WAITING_FOR_BUY_FILL

        with caplog.at_level(logging.ERROR):
            target = strategy.buy_target(layout.grid_levels[130.0], layout, LOGGER)

        assert target is None
        assert "is busy" in caplog.text

    def test_buy_target_below_the_bottom_level_is_none(self, caplog):
        strategy = HedgedGridStrategy()
        layout = _layout(strategy)

        with caplog.at_level(logging.ERROR):
            target = strategy.buy_target(layout.grid_levels[100.0], layout, LOGGER)

        assert target is None
        assert "No grid level next to 100.0" in caplog.text


class TestSimpleInitialPlan:
    @pytest.mark.parametrize(
        ("current_price", "expected"),
        [
            (
                125.0,
                [
                    (100.0, OrderSide.BUY),
                    (110.0, OrderSide.BUY),
                    (120.0, OrderSide.BUY),
                    (130.0, OrderSide.SELL),
                    (140.0, OrderSide.SELL),
                ],
            ),
            (135.0, [(100.0, OrderSide.BUY), (110.0, OrderSide.BUY), (120.0, OrderSide.BUY), (140.0, OrderSide.SELL)]),
            (105.0, [(100.0, OrderSide.BUY), (130.0, OrderSide.SELL), (140.0, OrderSide.SELL)]),
        ],
    )
    def test_buys_below_the_price_on_lower_levels_and_sells_above_on_upper_levels(self, current_price, expected):
        strategy = SimpleGridStrategy()

        plan = strategy.initial_order_plan(_layout(strategy), current_price)

        assert _prices(plan) == expected


class TestSimpleTargets:
    def test_sell_target_is_the_lowest_free_upper_level(self):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)

        assert strategy.sell_target(layout.grid_levels[100.0], layout, LOGGER) is layout.grid_levels[130.0]

    def test_sell_target_skips_a_busy_upper_level(self):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)
        layout.grid_levels[130.0].state = GridCycleState.WAITING_FOR_SELL_FILL

        assert strategy.sell_target(layout.grid_levels[100.0], layout, LOGGER) is layout.grid_levels[140.0]

    def test_sell_target_stacks_on_the_lowest_upper_level_when_all_are_busy(self, caplog):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)
        layout.grid_levels[130.0].state = GridCycleState.WAITING_FOR_SELL_FILL
        layout.grid_levels[140.0].state = GridCycleState.WAITING_FOR_SELL_FILL

        with caplog.at_level(logging.INFO):
            target = strategy.sell_target(layout.grid_levels[100.0], layout, LOGGER)

        assert target is layout.grid_levels[130.0]
        assert "stacking" in caplog.text

    def test_centre_level_is_a_buy_level_and_never_a_sell_target(self):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)
        layout.grid_levels[130.0].state = GridCycleState.WAITING_FOR_SELL_FILL
        layout.grid_levels[140.0].state = GridCycleState.WAITING_FOR_SELL_FILL

        assert 120.0 in layout.sorted_buy_grids
        assert strategy.sell_target(layout.grid_levels[110.0], layout, LOGGER).price == 130.0

    def test_buy_target_is_the_highest_free_lower_level(self):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)

        assert strategy.buy_target(layout.grid_levels[130.0], layout, LOGGER) is layout.grid_levels[120.0]

    def test_buy_target_skips_a_busy_lower_level(self):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)
        layout.grid_levels[120.0].state = GridCycleState.WAITING_FOR_BUY_FILL

        assert strategy.buy_target(layout.grid_levels[130.0], layout, LOGGER) is layout.grid_levels[110.0]

    def test_buy_target_none_when_every_lower_level_is_busy(self, caplog):
        strategy = SimpleGridStrategy()
        layout = _layout(strategy)
        for price in (100.0, 110.0, 120.0):
            layout.grid_levels[price].state = GridCycleState.WAITING_FOR_BUY_FILL

        with caplog.at_level(logging.INFO):
            target = strategy.buy_target(layout.grid_levels[130.0], layout, LOGGER)

        assert target is None
        assert "stay in fiat" in caplog.text
