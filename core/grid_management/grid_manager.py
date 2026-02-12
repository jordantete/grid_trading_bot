import logging

import numpy as np

from config.config_manager import ConfigManager
from core.domain.spacing_type import SpacingType
from core.domain.strategy_type import StrategyType

from ..order_handling.order import Order, OrderSide
from .grid_level import GridCycleState, GridLevel


class GridManager:
    def __init__(
        self,
        config_manager: ConfigManager,
        strategy_type: StrategyType,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_manager: ConfigManager = config_manager
        self.strategy_type: StrategyType = strategy_type
        self.price_grids: list[float]
        self.central_price: float
        self.sorted_buy_grids: list[float]
        self.sorted_sell_grids: list[float]
        self.grid_levels: dict[float, GridLevel] = {}

    def initialize_grids_and_levels(self) -> None:
        """
        Initializes the grid levels and assigns their respective states based on the chosen strategy.

        For the `SIMPLE_GRID` strategy:
        - Buy orders are placed on grid levels below the central price.
        - Sell orders are placed on grid levels above the central price.
        - Levels are initialized with `READY_TO_BUY` or `READY_TO_SELL` states.

        For the `HEDGED_GRID` strategy:
        - Grid levels are divided into buy levels (all except the top grid) and
        sell levels (all except the bottom grid).
        - Buy grid levels are initialized with `READY_TO_BUY`, except for the topmost grid.
        - Sell grid levels are initialized with `READY_TO_SELL`.
        """
        self.price_grids, self.central_price = self._calculate_price_grids_and_central_price()

        if self.strategy_type == StrategyType.SIMPLE_GRID:
            self.sorted_buy_grids = [price_grid for price_grid in self.price_grids if price_grid <= self.central_price]
            self.sorted_sell_grids = [price_grid for price_grid in self.price_grids if price_grid > self.central_price]
            self.grid_levels = {
                price: GridLevel(
                    price,
                    GridCycleState.READY_TO_BUY if price <= self.central_price else GridCycleState.READY_TO_SELL,
                )
                for price in self.price_grids
            }

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            self.sorted_buy_grids = self.price_grids[:-1]  # All except the top grid
            self.sorted_sell_grids = self.price_grids[1:]  # All except the bottom grid
            self.grid_levels = {
                price: GridLevel(
                    price,
                    GridCycleState.READY_TO_BUY_OR_SELL
                    if price != self.price_grids[-1]
                    else GridCycleState.READY_TO_SELL,
                )
                for price in self.price_grids
            }
        self.logger.info(f"Grids and levels initialized. Central price: {self.central_price}")
        self.logger.info(f"Price grids: {self.price_grids}")
        self.logger.info(f"Buy grids: {self.sorted_buy_grids}")
        self.logger.info(f"Sell grids: {self.sorted_sell_grids}")
        self.logger.info(f"Grid levels: {self.grid_levels}")

    def get_trigger_price(self) -> float:
        return self.central_price

    def get_order_size_for_grid_level(
        self,
        total_balance: float,
        current_price: float,
    ) -> float:
        """
        Calculates the order size for a grid level based on the total balance, total grids, and current price.

        Args:
            total_balance: The total portfolio value in fiat.
            current_price: The current price of the trading pair.

        Returns:
            The calculated order size as a float.
        """
        total_grids = len(self.grid_levels)
        return total_balance / total_grids / current_price

    def get_initial_order_quantity(
        self,
        current_fiat_balance: float,
        current_crypto_balance: float,
        current_price: float,
    ) -> float:
        """
        Calculates the initial quantity of crypto to purchase for grid initialization.

        Args:
            current_fiat_balance (float): The current fiat balance.
            current_crypto_balance (float): The current crypto balance.
            current_price (float): The current market price of the crypto.

        Returns:
            float: The quantity of crypto to purchase.
        """
        current_crypto_value_in_fiat = current_crypto_balance * current_price
        total_portfolio_value = current_fiat_balance + current_crypto_value_in_fiat
        target_crypto_allocation_in_fiat = total_portfolio_value / 2  # Allocate 50% of balance for initial buy
        fiat_to_allocate_for_purchase = target_crypto_allocation_in_fiat - current_crypto_value_in_fiat
        fiat_to_allocate_for_purchase = max(0, min(fiat_to_allocate_for_purchase, current_fiat_balance))
        return fiat_to_allocate_for_purchase / current_price

    def pair_grid_levels(
        self,
        source_grid_level: GridLevel,
        target_grid_level: GridLevel,
        pairing_type: str,
    ) -> None:
        """
        Dynamically pairs grid levels for buy or sell purposes.

        Args:
            source_grid_level: The grid level initiating the pairing.
            target_grid_level: The grid level being paired.
            pairing_type: "buy" or "sell" to specify the type of pairing.
        """
        if pairing_type == "buy":
            source_grid_level.paired_buy_level = target_grid_level
            target_grid_level.paired_sell_level = source_grid_level
            self.logger.info(
                f"Paired sell grid level {source_grid_level.price} with buy grid level {target_grid_level.price}.",
            )

        elif pairing_type == "sell":
            source_grid_level.paired_sell_level = target_grid_level
            target_grid_level.paired_buy_level = source_grid_level
            self.logger.info(
                f"Paired buy grid level {source_grid_level.price} with sell grid level {target_grid_level.price}.",
            )

        else:
            raise ValueError(f"Invalid pairing type: {pairing_type}. Must be 'buy' or 'sell'.")

    def get_paired_sell_level(
        self,
        buy_grid_level: GridLevel,
    ) -> GridLevel | None:
        """
        Determines the paired sell level for a given buy grid level based on the strategy type.

        Args:
            buy_grid_level: The buy grid level for which the paired sell level is required.

        Returns:
            The paired sell grid level, or None if no valid level exists.
        """
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            self.logger.info(f"Looking for paired sell level for buy level at {buy_grid_level}")
            self.logger.info(f"Available sell grids: {self.sorted_sell_grids}")

            for sell_price in self.sorted_sell_grids:
                sell_level = self.grid_levels[sell_price]
                self.logger.info(f"Checking sell level {sell_price}, state: {sell_level.state}")

                if sell_level and not self.can_place_order(sell_level, OrderSide.SELL):
                    self.logger.info(
                        f"Skipping sell level {sell_price} - cannot place order. State: {sell_level.state}",
                    )
                    continue

                if sell_price > buy_grid_level.price:
                    self.logger.info(f"Paired sell level found at {sell_price} for buy level {buy_grid_level}.")
                    return sell_level

            self.logger.warning(f"No suitable sell level found above {buy_grid_level}")
            return None

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            self.logger.info(f"Available price grids: {self.price_grids}")
            sorted_prices = sorted(self.price_grids)
            current_index = sorted_prices.index(buy_grid_level.price)
            self.logger.info(f"Current index of buy level {buy_grid_level.price}: {current_index}")

            if current_index + 1 < len(sorted_prices):
                paired_sell_price = sorted_prices[current_index + 1]
                sell_level = self.grid_levels[paired_sell_price]
                self.logger.info(
                    f"Paired sell level for buy level {buy_grid_level.price} is at "
                    f"{paired_sell_price} (state: {sell_level.state})",
                )
                return sell_level

            self.logger.warning(f"No suitable sell level found for buy grid level {buy_grid_level}")
            return None

        else:
            self.logger.error(f"Unsupported strategy type: {self.strategy_type}")
            return None

    def get_grid_level_below(self, grid_level: GridLevel) -> GridLevel | None:
        """
        Returns the grid level immediately below the given grid level.

        Args:
            grid_level: The current grid level.

        Returns:
            The grid level below the given grid level, or None if it doesn't exist.
        """
        sorted_levels = sorted(self.grid_levels.keys())
        current_index = sorted_levels.index(grid_level.price)

        if current_index > 0:
            lower_price = sorted_levels[current_index - 1]
            return self.grid_levels[lower_price]
        return None

    def mark_order_pending(
        self,
        grid_level: GridLevel,
        order: Order,
    ) -> None:
        """
        Marks a grid level as having a pending order (buy or sell).

        Args:
            grid_level: The grid level to update.
            order: The Order object representing the pending order.
            order_side: The side of the order (buy or sell).
        """
        grid_level.add_order(order)

        if order.side == OrderSide.BUY:
            grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL
            self.logger.info(f"Buy order placed and marked as pending at grid level {grid_level.price}.")
        elif order.side == OrderSide.SELL:
            grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL
            self.logger.info(f"Sell order placed and marked as pending at grid level {grid_level.price}.")

    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> None:
        """
        Marks the completion of an order (buy or sell) and transitions the grid level.

        Args:
            grid_level: The grid level where the order was completed.
            order_side: The side of the completed order (buy or sell).
        """
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            if order_side == OrderSide.BUY:
                grid_level.state = GridCycleState.READY_TO_SELL
                self.logger.info(
                    f"Buy order completed at grid level {grid_level.price}. Transitioning to READY_TO_SELL.",
                )
            elif order_side == OrderSide.SELL:
                grid_level.state = GridCycleState.READY_TO_BUY
                self.logger.info(
                    f"Sell order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY.",
                )

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            if order_side == OrderSide.BUY:
                grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
                self.logger.info(
                    f"Buy order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY_OR_SELL.",
                )

                # Transition the paired buy level to "READY_TO_SELL"
                if grid_level.paired_sell_level:
                    grid_level.paired_sell_level.state = GridCycleState.READY_TO_SELL
                    self.logger.info(
                        f"Paired sell grid level {grid_level.paired_sell_level.price} transitioned to READY_TO_SELL.",
                    )

            elif order_side == OrderSide.SELL:
                grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
                self.logger.info(
                    f"Sell order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY_OR_SELL.",
                )

                # Transition the paired buy level to "READY_TO_BUY"
                if grid_level.paired_buy_level:
                    grid_level.paired_buy_level.state = GridCycleState.READY_TO_BUY
                    self.logger.info(
                        f"Paired buy grid level {grid_level.paired_buy_level.price} transitioned to READY_TO_BUY.",
                    )

        else:
            self.logger.error("Unexpected strategy type")

    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> bool:
        """
        Determines if an order can be placed on the given grid level for the current strategy.

        Args:
            grid_level: The grid level being evaluated.
            order_side: The side of the order (buy or sell).

        Returns:
            bool: True if the order can be placed, False otherwise.
        """
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            if order_side == OrderSide.BUY:
                return grid_level.state == GridCycleState.READY_TO_BUY
            elif order_side == OrderSide.SELL:
                return grid_level.state == GridCycleState.READY_TO_SELL

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            if order_side == OrderSide.BUY:
                return grid_level.state in {GridCycleState.READY_TO_BUY, GridCycleState.READY_TO_BUY_OR_SELL}
            elif order_side == OrderSide.SELL:
                return grid_level.state in {GridCycleState.READY_TO_SELL, GridCycleState.READY_TO_BUY_OR_SELL}

        else:
            return False

    def _extract_grid_config(self) -> tuple[float, float, int, str]:
        """
        Extracts grid configuration parameters from the configuration manager.
        """
        bottom_range = self.config_manager.get_bottom_range()
        top_range = self.config_manager.get_top_range()
        num_grids = self.config_manager.get_num_grids()
        spacing_type = self.config_manager.get_spacing_type()
        return bottom_range, top_range, num_grids, spacing_type

    def _calculate_price_grids_and_central_price(self) -> tuple[list[float], float]:
        """
        Calculates price grids and the central price based on the configuration.

        Returns:
            Tuple[List[float], float]: A tuple containing:
                - grids (List[float]): The list of calculated grid prices.
                - central_price (float): The central price of the grid.
        """
        bottom_range, top_range, num_grids, spacing_type = self._extract_grid_config()

        if spacing_type == SpacingType.ARITHMETIC:
            grids = np.linspace(bottom_range, top_range, num_grids)
            central_price = (top_range + bottom_range) / 2

        elif spacing_type == SpacingType.GEOMETRIC:
            grids = []
            ratio = (top_range / bottom_range) ** (1 / (num_grids - 1))
            current_price = bottom_range

            for _ in range(num_grids):
                grids.append(current_price)
                current_price *= ratio

            central_index = len(grids) // 2
            if num_grids % 2 == 0:
                central_price = (grids[central_index - 1] + grids[central_index]) / 2
            else:
                central_price = grids[central_index]

        else:
            raise ValueError(f"Unsupported spacing type: {spacing_type}")

        return grids, central_price
