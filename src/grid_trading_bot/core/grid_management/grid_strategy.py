from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..order_handling.order import OrderSide
from .grid_level import GridCycleState, GridLevel


@dataclass(frozen=True)
class GridLayout:
    """Read-only view of the grid geometry handed to strategies."""

    sorted_prices: list[float]
    price_index_map: dict[float, int]
    grid_levels: dict[float, GridLevel]
    sorted_buy_grids: list[float]
    sorted_sell_grids: list[float]

    def neighbour(self, grid_level: GridLevel, offset: int) -> GridLevel | None:
        index = self.price_index_map[grid_level.price] + offset
        if 0 <= index < len(self.sorted_prices):
            return self.grid_levels[self.sorted_prices[index]]
        return None


class GridStrategy(ABC):
    """Abstract base class for grid strategy behavior that varies by strategy type."""

    @abstractmethod
    def initialize_levels(
        self,
        price_grids: list[float],
        central_price: float,
    ) -> tuple[list[float], list[float], dict[float, GridLevel]]:
        """
        Initializes grid levels based on the strategy type.

        Returns:
            A tuple of (buy_grids, sell_grids, grid_levels dict).
        """
        pass

    @abstractmethod
    def initial_order_plan(self, layout: GridLayout, current_price: float) -> list[tuple[GridLevel, OrderSide]]:
        """Levels that receive an order when the grid is armed at current_price: buys then sells, ascending."""

    @abstractmethod
    def sell_target(self, buy_level: GridLevel, layout: GridLayout, logger) -> GridLevel | None:
        """Level that takes the sell for crypto bought at buy_level, or None when no sell can be placed."""

    @abstractmethod
    def buy_target(self, sell_level: GridLevel, layout: GridLayout, logger) -> GridLevel | None:
        """Level that takes the rebuy for the proceeds of sell_level, or None when they stay in fiat."""

    @abstractmethod
    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> bool:
        """Determines if an order can be placed on the given grid level."""
        pass

    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
        has_open_orders: bool,
        logger,
    ) -> None:
        """Returns the filled level to its home state unless it still holds open orders."""
        self._release(grid_level, has_open_orders, logger, f"{order_side.value.capitalize()} order completed")

    def rollback_order_placement(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
        has_open_orders: bool,
        logger,
    ) -> None:
        """Reverts a level after its pending order was cancelled, unless it still holds open orders."""
        self._release(grid_level, has_open_orders, logger, f"{order_side.value.capitalize()} order cancelled")

    @staticmethod
    def _release(grid_level: GridLevel, has_open_orders: bool, logger, event: str) -> None:
        if has_open_orders:
            logger.info(
                f"{event} at grid level {grid_level.price}; other orders remain, it stays {grid_level.state.name}."
            )
            return
        grid_level.state = grid_level.home_state
        logger.info(f"{event} at grid level {grid_level.price}. Transitioning to {grid_level.state.name}.")


class SimpleGridStrategy(GridStrategy):
    def initialize_levels(
        self,
        price_grids: list[float],
        central_price: float,
    ) -> tuple[list[float], list[float], dict[float, GridLevel]]:
        buy_grids = [p for p in price_grids if p <= central_price]
        sell_grids = [p for p in price_grids if p > central_price]
        grid_levels = {
            price: GridLevel(
                price,
                GridCycleState.READY_TO_BUY if price <= central_price else GridCycleState.READY_TO_SELL,
            )
            for price in price_grids
        }
        return buy_grids, sell_grids, grid_levels

    def initial_order_plan(self, layout: GridLayout, current_price: float) -> list[tuple[GridLevel, OrderSide]]:
        buys = [(layout.grid_levels[p], OrderSide.BUY) for p in layout.sorted_buy_grids if p < current_price]
        sells = [(layout.grid_levels[p], OrderSide.SELL) for p in layout.sorted_sell_grids if p > current_price]
        return buys + sells

    def sell_target(self, buy_level: GridLevel, layout: GridLayout, logger) -> GridLevel | None:
        if not layout.sorted_sell_grids:
            logger.error(f"No sell level in the grid; sell for buy level {buy_level.price} not placed.")
            return None
        sell_levels = [layout.grid_levels[p] for p in layout.sorted_sell_grids]
        for level in sell_levels:
            if self.can_place_order(level, OrderSide.SELL):
                return level
        logger.info(
            f"Every sell level holds an order; stacking the sell for buy level {buy_level.price} "
            f"on {sell_levels[0].price}."
        )
        return sell_levels[0]

    def buy_target(self, sell_level: GridLevel, layout: GridLayout, logger) -> GridLevel | None:
        for price in reversed(layout.sorted_buy_grids):
            level = layout.grid_levels[price]
            if self.can_place_order(level, OrderSide.BUY):
                return level
        logger.info(f"No free buy level for the proceeds of sell level {sell_level.price}; they stay in fiat.")
        return None

    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> bool:
        if order_side == OrderSide.BUY:
            return grid_level.state == GridCycleState.READY_TO_BUY
        elif order_side == OrderSide.SELL:
            return grid_level.state == GridCycleState.READY_TO_SELL
        return False


class HedgedGridStrategy(GridStrategy):
    def initialize_levels(
        self,
        price_grids: list[float],
        central_price: float,
    ) -> tuple[list[float], list[float], dict[float, GridLevel]]:
        buy_grids = price_grids[:-1]  # All except the top grid
        sell_grids = price_grids[1:]  # All except the bottom grid
        grid_levels = {
            price: GridLevel(
                price,
                GridCycleState.READY_TO_BUY_OR_SELL if price != price_grids[-1] else GridCycleState.READY_TO_SELL,
            )
            for price in price_grids
        }
        return buy_grids, sell_grids, grid_levels

    def initial_order_plan(self, layout: GridLayout, current_price: float) -> list[tuple[GridLevel, OrderSide]]:
        # The level nearest the price stays free so every fill finds its neighbour empty.
        free_price = min(layout.sorted_prices, key=lambda price: (abs(price - current_price), price))
        buys = [
            (layout.grid_levels[p], OrderSide.BUY)
            for p in layout.sorted_buy_grids
            if p < current_price and p != free_price
        ]
        sells = [
            (layout.grid_levels[p], OrderSide.SELL)
            for p in layout.sorted_sell_grids
            if p > current_price and p != free_price
        ]
        return buys + sells

    def sell_target(self, buy_level: GridLevel, layout: GridLayout, logger) -> GridLevel | None:
        return self._free_neighbour(buy_level, layout, 1, OrderSide.SELL, logger)

    def buy_target(self, sell_level: GridLevel, layout: GridLayout, logger) -> GridLevel | None:
        return self._free_neighbour(sell_level, layout, -1, OrderSide.BUY, logger)

    def _free_neighbour(
        self,
        filled_level: GridLevel,
        layout: GridLayout,
        offset: int,
        side: OrderSide,
        logger,
    ) -> GridLevel | None:
        target = layout.neighbour(filled_level, offset)
        if target is None:
            logger.error(f"No grid level next to {filled_level.price} for the {side.value}; order not placed.")
            return None
        if not self.can_place_order(target, side):
            logger.error(
                f"{side.value.capitalize()} target {target.price} for grid level {filled_level.price} is busy "
                f"({target.state.name}); order not placed."
            )
            return None
        return target

    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> bool:
        if order_side == OrderSide.BUY:
            return grid_level.state in {GridCycleState.READY_TO_BUY, GridCycleState.READY_TO_BUY_OR_SELL}
        elif order_side == OrderSide.SELL:
            return grid_level.state in {GridCycleState.READY_TO_SELL, GridCycleState.READY_TO_BUY_OR_SELL}
        return False
