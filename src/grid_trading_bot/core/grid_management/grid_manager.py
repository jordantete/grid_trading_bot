import logging

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.domain.spacing_type import SpacingType
from grid_trading_bot.core.domain.strategy_type import StrategyType

from ..order_handling.order import Order, OrderSide
from .grid_level import GridCycleState, GridLevel
from .grid_strategy import GridStrategy, HedgedGridStrategy, SimpleGridStrategy


class GridManager:
    def __init__(
        self,
        config_manager: ConfigManager,
        strategy_type: StrategyType,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_manager: ConfigManager = config_manager
        self.strategy_type: StrategyType = strategy_type
        self.grid_strategy: GridStrategy = self._create_grid_strategy(strategy_type)
        self.price_grids: list[float]
        self.central_price: float
        self.sorted_buy_grids: list[float]
        self.sorted_sell_grids: list[float]
        self.grid_levels: dict[float, GridLevel] = {}
        self._sorted_prices: list[float] = []
        self._price_index_map: dict[float, int] = {}

    @staticmethod
    def _create_grid_strategy(strategy_type: StrategyType) -> GridStrategy:
        if strategy_type == StrategyType.SIMPLE_GRID:
            return SimpleGridStrategy()
        elif strategy_type == StrategyType.HEDGED_GRID:
            return HedgedGridStrategy()
        else:
            raise ValueError(f"Unsupported strategy type: {strategy_type}")

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
        self.sorted_buy_grids, self.sorted_sell_grids, self.grid_levels = self.grid_strategy.initialize_levels(
            self.price_grids, self.central_price
        )
        self._sorted_prices = sorted(self.price_grids)
        self._price_index_map = {p: i for i, p in enumerate(self._sorted_prices)}
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
        result = self.grid_strategy.get_paired_sell_level(
            buy_grid_level,
            self.grid_levels,
            self.sorted_sell_grids,
            self._sorted_prices,
            self._price_index_map,
            self.can_place_order,
        )
        if result is None:
            self.logger.warning(f"No suitable sell level found for buy grid level {buy_grid_level}")
        return result

    def get_grid_level_below(self, grid_level: GridLevel) -> GridLevel | None:
        """
        Returns the grid level immediately below the given grid level.

        Args:
            grid_level: The current grid level.

        Returns:
            The grid level below the given grid level, or None if it doesn't exist.
        """
        current_index = self._price_index_map[grid_level.price]

        if current_index > 0:
            lower_price = self._sorted_prices[current_index - 1]
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
        self.grid_strategy.complete_order(grid_level, order_side, self.logger)

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
        return self.grid_strategy.can_place_order(grid_level, order_side)

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
            grids = [bottom_range + i * (top_range - bottom_range) / (num_grids - 1) for i in range(num_grids)]
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
