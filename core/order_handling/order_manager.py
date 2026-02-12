import logging

from config.trading_mode import TradingMode
from core.bot_management.event_bus import EventBus, Events
from core.bot_management.notification.notification_content import NotificationType
from core.bot_management.notification.notification_handler import NotificationHandler
from core.domain.strategy_type import StrategyType

from ..grid_management.grid_level import GridLevel
from ..grid_management.grid_manager import GridManager
from ..order_handling.balance_tracker import BalanceTracker
from ..order_handling.order_book import OrderBook
from ..validation.order_validator import OrderValidator
from .exceptions import OrderExecutionFailedError
from .execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from .order import Order, OrderSide
from .order_simulator import OrderSimulator


class OrderManager:
    def __init__(
        self,
        grid_manager: GridManager,
        order_validator: OrderValidator,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        event_bus: EventBus,
        order_execution_strategy: OrderExecutionStrategyInterface,
        notification_handler: NotificationHandler,
        order_simulator: OrderSimulator,
        trading_mode: TradingMode,
        trading_pair: str,
        strategy_type: StrategyType,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.order_validator = order_validator
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.event_bus = event_bus
        self.order_execution_strategy = order_execution_strategy
        self.notification_handler = notification_handler
        self.order_simulator = order_simulator
        self.trading_mode: TradingMode = trading_mode
        self.trading_pair = trading_pair
        self.strategy_type: StrategyType = strategy_type
        self.event_bus.subscribe(Events.ORDER_FILLED, self._on_order_filled)
        self.event_bus.subscribe(Events.ORDER_CANCELLED, self._on_order_cancelled)

    async def initialize_grid_orders(
        self,
        current_price: float,
    ):
        """
        Places initial buy and sell orders for grid levels around the current price.
        """
        await self._initialize_buy_orders(current_price)
        await self._initialize_sell_orders(current_price)

    async def _initialize_buy_orders(self, current_price: float) -> None:
        for price in self.grid_manager.sorted_buy_grids:
            if price >= current_price:
                self.logger.info(f"Skipping grid level at price: {price} for BUY order: Above current price.")
                continue

            grid_level = self.grid_manager.grid_levels[price]
            total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price)

            if self.grid_manager.can_place_order(grid_level, OrderSide.BUY):
                try:
                    adjusted_buy_order_quantity = self.order_validator.adjust_and_validate_buy_quantity(
                        balance=self.balance_tracker.balance,
                        order_quantity=order_quantity,
                        price=price,
                    )

                    self.logger.info(
                        f"Placing initial buy limit order at grid level {price} for "
                        f"{adjusted_buy_order_quantity} {self.trading_pair}.",
                    )
                    order = await self.order_execution_strategy.execute_limit_order(
                        OrderSide.BUY,
                        self.trading_pair,
                        adjusted_buy_order_quantity,
                        price,
                    )

                    if order is None:
                        raise OrderExecutionFailedError(
                            f"Buy order at {price} returned None",
                        )

                    await self.balance_tracker.reserve_funds_for_buy(adjusted_buy_order_quantity * price)
                    self.grid_manager.mark_order_pending(grid_level, order)
                    self.order_book.add_order(order, grid_level)

                except (OrderExecutionFailedError, Exception) as e:
                    await self._handle_order_init_error("buy", price, e)

    async def _initialize_sell_orders(self, current_price: float) -> None:
        for price in self.grid_manager.sorted_sell_grids:
            if price <= current_price:
                self.logger.info(
                    f"Skipping grid level at price: {price} for SELL order: Below or equal to current price.",
                )
                continue

            grid_level = self.grid_manager.grid_levels[price]
            total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price)

            if self.grid_manager.can_place_order(grid_level, OrderSide.SELL):
                try:
                    adjusted_sell_order_quantity = self.order_validator.adjust_and_validate_sell_quantity(
                        crypto_balance=self.balance_tracker.crypto_balance,
                        order_quantity=order_quantity,
                    )

                    self.logger.info(
                        f"Placing initial sell limit order at grid level {price} for "
                        f"{adjusted_sell_order_quantity} {self.trading_pair}.",
                    )
                    order = await self.order_execution_strategy.execute_limit_order(
                        OrderSide.SELL,
                        self.trading_pair,
                        adjusted_sell_order_quantity,
                        price,
                    )

                    if order is None:
                        raise OrderExecutionFailedError(
                            f"Sell order at {price} returned None",
                        )

                    await self.balance_tracker.reserve_funds_for_sell(adjusted_sell_order_quantity)
                    self.grid_manager.mark_order_pending(grid_level, order)
                    self.order_book.add_order(order, grid_level)

                except (OrderExecutionFailedError, Exception) as e:
                    await self._handle_order_init_error("sell", price, e)

    async def _handle_order_init_error(self, side: str, price: float, error: Exception) -> None:
        if isinstance(error, OrderExecutionFailedError):
            self.logger.error(f"Failed to initialize {side} order at grid level {price} - {error!s}", exc_info=True)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Error while placing initial {side} order. {error}",
            )
        else:
            self.logger.error(
                f"Unexpected error during {side} order initialization at grid level {price}: {error}",
                exc_info=True,
            )
            await self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details=f"Error while placing initial {side} order: {error!s}",
            )

    async def _on_order_cancelled(
        self,
        order: Order,
    ) -> None:
        """
        Handles cancelled orders.

        Args:
            order: The cancelled Order instance.
        """
        self.logger.warning(f"Order cancelled at grid level — re-placement not yet implemented: {order}")
        await self.notification_handler.async_send_notification(
            NotificationType.ORDER_CANCELLED,
            order_details=str(order),
        )

    async def _on_order_filled(
        self,
        order: Order,
    ) -> None:
        """
        Handles filled orders and places paired orders as needed.

        Args:
            order: The filled Order instance.
        """
        try:
            grid_level = self.order_book.get_grid_level_for_order(order)

            if not grid_level:
                self.logger.error(
                    f"Could not handle Order completion - No grid level found for the given filled order {order}",
                )
                return

            await self._handle_order_completion(order, grid_level)

        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while handling filled order - {e!s}", exc_info=True)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
            )

        except Exception as e:
            self.logger.error(f"Error while handling filled order {order.identifier}: {e}", exc_info=True)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
            )

    async def _handle_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        """
        Handles the completion of an order (buy or sell).

        Args:
            order: The filled Order instance.
            grid_level: The grid level associated with the filled order.
        """
        if order.side == OrderSide.BUY:
            await self._handle_buy_order_completion(order, grid_level)

        elif order.side == OrderSide.SELL:
            await self._handle_sell_order_completion(order, grid_level)

    async def _handle_buy_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        """
        Handles the completion of a buy order.

        Args:
            order: The completed Buy Order instance.
            grid_level: The grid level associated with the completed buy order.
        """
        self.logger.info(f"Buy order completed at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, OrderSide.BUY)
        paired_sell_level = self.grid_manager.get_paired_sell_level(grid_level)

        if paired_sell_level and self.grid_manager.can_place_order(paired_sell_level, OrderSide.SELL):
            await self._place_order(OrderSide.SELL, grid_level, paired_sell_level, order.filled)
        else:
            self.logger.warning(
                f"No valid sell grid level found for buy grid level {grid_level}. Skipping sell order placement.",
            )

    async def _handle_sell_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        """
        Handles the completion of a sell order.

        Args:
            order: The completed Sell Order instance.
            grid_level: The grid level associated with the completed sell order.
        """
        self.logger.info(f"Sell order completed at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, OrderSide.SELL)
        paired_buy_level = self._get_or_create_paired_buy_level(grid_level)

        if paired_buy_level:
            await self._place_order(OrderSide.BUY, grid_level, paired_buy_level, order.filled)
        else:
            self.logger.error(f"Failed to find or create a paired buy grid level for grid level {grid_level}.")

    def _get_or_create_paired_buy_level(self, sell_grid_level: GridLevel) -> GridLevel | None:
        """
        Retrieves or creates a paired buy grid level for the given sell grid level.

        Args:
            sell_grid_level: The sell grid level to find a paired buy level for.

        Returns:
            The paired buy grid level, or None if a valid level cannot be found.
        """
        paired_buy_level = sell_grid_level.paired_buy_level

        if paired_buy_level and self.grid_manager.can_place_order(paired_buy_level, OrderSide.BUY):
            self.logger.info(f"Found valid paired buy level {paired_buy_level} for sell level {sell_grid_level}.")
            return paired_buy_level

        fallback_buy_level = self.grid_manager.get_grid_level_below(sell_grid_level)

        if fallback_buy_level:
            self.logger.info(f"Paired fallback buy level {fallback_buy_level} with sell level {sell_grid_level}.")
            return fallback_buy_level

        self.logger.warning(f"No valid fallback buy level found below sell level {sell_grid_level}.")
        return None

    async def _place_order(
        self,
        order_side: OrderSide,
        source_grid_level: GridLevel,
        target_grid_level: GridLevel,
        quantity: float,
    ) -> None:
        """
        Places an order at the specified target grid level and pairs it with the source level.

        Args:
            order_side: The side of the order (BUY or SELL).
            source_grid_level: The grid level that triggered this order.
            target_grid_level: The grid level to place the order on.
            quantity: The quantity of the order.
        """
        if order_side == OrderSide.BUY:
            adjusted_quantity = self.order_validator.adjust_and_validate_buy_quantity(
                self.balance_tracker.balance,
                quantity,
                target_grid_level.price,
            )
        else:
            adjusted_quantity = self.order_validator.adjust_and_validate_sell_quantity(
                self.balance_tracker.crypto_balance,
                quantity,
            )

        order = await self.order_execution_strategy.execute_limit_order(
            order_side,
            self.trading_pair,
            adjusted_quantity,
            target_grid_level.price,
        )

        if order:
            pairing_type = "buy" if order_side == OrderSide.BUY else "sell"
            self.grid_manager.pair_grid_levels(source_grid_level, target_grid_level, pairing_type=pairing_type)
            if order_side == OrderSide.BUY:
                await self.balance_tracker.reserve_funds_for_buy(order.amount * target_grid_level.price)
            else:
                await self.balance_tracker.reserve_funds_for_sell(order.amount)
            self.grid_manager.mark_order_pending(target_grid_level, order)
            self.order_book.add_order(order, target_grid_level)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_PLACED,
                order_details=str(order),
            )
        else:
            self.logger.error(
                f"Failed to place {order_side.value} order at grid level {target_grid_level}",
            )

    async def perform_initial_purchase(
        self,
        current_price: float,
    ) -> None:
        """
        Handles the initial crypto purchase for grid trading strategy if required.

        Args:
            current_price: The current price of the trading pair.
        """
        initial_quantity = self.grid_manager.get_initial_order_quantity(
            current_fiat_balance=self.balance_tracker.balance,
            current_crypto_balance=self.balance_tracker.crypto_balance,
            current_price=current_price,
        )

        if initial_quantity <= 0:
            self.logger.warning("Initial purchase quantity is zero or negative. Skipping initial purchase.")
            return

        self.logger.info(f"Performing initial crypto purchase: {initial_quantity} at price {current_price}.")

        try:
            buy_order = await self.order_execution_strategy.execute_market_order(
                OrderSide.BUY,
                self.trading_pair,
                initial_quantity,
                current_price,
            )
            self.logger.info(f"Initial crypto purchase completed. Order details: {buy_order}")
            self.order_book.add_order(buy_order)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_PLACED,
                order_details=f"Initial purchase done: {buy_order!s}",
            )

            if self.trading_mode == TradingMode.BACKTEST:
                await self.order_simulator._simulate_fill(buy_order, buy_order.timestamp)
            else:
                # Update fiat and crypto balance in LIVE & PAPER_TRADING modes without simulating it
                await self.balance_tracker.update_after_initial_purchase(initial_order=buy_order)

        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while executing initial purchase - {e!s}", exc_info=True)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Error while performing initial purchase. {e}",
            )

        except Exception as e:
            self.logger.error(
                f"Failed to perform initial purchase at current_price: {current_price} - error: {e}",
                exc_info=True,
            )
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Error while performing initial purchase. {e}",
            )

    async def execute_take_profit_or_stop_loss_order(
        self,
        current_price: float,
        take_profit_order: bool = False,
        stop_loss_order: bool = False,
    ) -> None:
        """
        Executes a sell order triggered by either a take-profit or stop-loss event.

        This method checks whether a take-profit or stop-loss condition has been met
        and places a market sell order accordingly. It uses the crypto balance tracked
        by the `BalanceTracker` and sends notifications upon success or failure.

        Args:
            current_price (float): The current market price triggering the event.
            take_profit_order (bool): Indicates whether this is a take-profit event.
            stop_loss_order (bool): Indicates whether this is a stop-loss event.
        """
        if not (take_profit_order or stop_loss_order):
            self.logger.warning("No take profit or stop loss action specified.")
            return

        event = "Take profit" if take_profit_order else "Stop loss"
        try:
            quantity = self.balance_tracker.crypto_balance
            order = await self.order_execution_strategy.execute_market_order(
                OrderSide.SELL,
                self.trading_pair,
                quantity,
                current_price,
            )

            if not order:
                raise OrderExecutionFailedError(
                    f"{event} order execution returned None at price {current_price}",
                )

            self.order_book.add_order(order)
            await self.notification_handler.async_send_notification(
                NotificationType.TAKE_PROFIT_TRIGGERED if take_profit_order else NotificationType.STOP_LOSS_TRIGGERED,
                order_details=str(order),
            )
            self.logger.info(f"{event} triggered at {current_price} and sell order executed.")

        except OrderExecutionFailedError as e:
            self.logger.error(f"Order execution failed: {e!s}")
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed to place {event} order: {e}",
            )

        except Exception as e:
            self.logger.error(f"Failed to execute {event} sell order at {current_price}: {e}")
            await self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details=f"Failed to place {event} order: {e}",
            )
