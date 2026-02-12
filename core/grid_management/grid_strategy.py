from abc import ABC, abstractmethod

from ..order_handling.order import OrderSide
from .grid_level import GridCycleState, GridLevel


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
    def get_paired_sell_level(
        self,
        buy_grid_level: GridLevel,
        grid_levels: dict[float, GridLevel],
        sorted_sell_grids: list[float],
        sorted_prices: list[float],
        price_index_map: dict[float, int],
        can_place_order_fn,
    ) -> GridLevel | None:
        """Determines the paired sell level for a given buy grid level."""
        pass

    @abstractmethod
    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
        logger,
    ) -> None:
        """Marks the completion of an order and transitions the grid level."""
        pass

    @abstractmethod
    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> bool:
        """Determines if an order can be placed on the given grid level."""
        pass


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

    def get_paired_sell_level(
        self,
        buy_grid_level: GridLevel,
        grid_levels: dict[float, GridLevel],
        sorted_sell_grids: list[float],
        sorted_prices: list[float],
        price_index_map: dict[float, int],
        can_place_order_fn,
    ) -> GridLevel | None:
        for sell_price in sorted_sell_grids:
            sell_level = grid_levels[sell_price]
            if not can_place_order_fn(sell_level, OrderSide.SELL):
                continue
            if sell_price > buy_grid_level.price:
                return sell_level
        return None

    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
        logger,
    ) -> None:
        if order_side == OrderSide.BUY:
            grid_level.state = GridCycleState.READY_TO_SELL
            logger.info(
                f"Buy order completed at grid level {grid_level.price}. Transitioning to READY_TO_SELL.",
            )
        elif order_side == OrderSide.SELL:
            grid_level.state = GridCycleState.READY_TO_BUY
            logger.info(
                f"Sell order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY.",
            )

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

    def get_paired_sell_level(
        self,
        buy_grid_level: GridLevel,
        grid_levels: dict[float, GridLevel],
        sorted_sell_grids: list[float],
        sorted_prices: list[float],
        price_index_map: dict[float, int],
        can_place_order_fn,
    ) -> GridLevel | None:
        current_index = price_index_map[buy_grid_level.price]
        if current_index + 1 < len(sorted_prices):
            paired_sell_price = sorted_prices[current_index + 1]
            return grid_levels[paired_sell_price]
        return None

    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
        logger,
    ) -> None:
        if order_side == OrderSide.BUY:
            grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
            logger.info(
                f"Buy order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY_OR_SELL.",
            )
            if grid_level.paired_sell_level:
                grid_level.paired_sell_level.state = GridCycleState.READY_TO_SELL
                logger.info(
                    f"Paired sell grid level {grid_level.paired_sell_level.price} transitioned to READY_TO_SELL.",
                )

        elif order_side == OrderSide.SELL:
            grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
            logger.info(
                f"Sell order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY_OR_SELL.",
            )
            if grid_level.paired_buy_level:
                grid_level.paired_buy_level.state = GridCycleState.READY_TO_BUY
                logger.info(
                    f"Paired buy grid level {grid_level.paired_buy_level.price} transitioned to READY_TO_BUY.",
                )

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
