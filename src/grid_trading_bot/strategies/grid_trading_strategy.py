from abc import abstractmethod
import asyncio
from contextvars import ContextVar
import logging
import math

import pandas as pd

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.schema import TrailingTrigger
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.exceptions import BookingError
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.exceptions import GridFeasibilityError
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.risk_management.trailing_stop_loss import TrailingStopLoss
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.strategies.performance_metrics import PerformanceMetrics
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer

from .trading_strategy_interface import TradingStrategyInterface

# Tasks started inside a ticker update copy the context, so a stop published from the update sees True.
_IN_TICK: ContextVar[bool] = ContextVar("in_ticker_update", default=False)


class GridTradingStrategy(TradingStrategyInterface):
    """
    Grid logic shared by the backtest and the live strategies: grid initialisation, TP/SL, trailing stop, regrid,
    state export and restore. Subclasses supply the trading session, the report data and the plot.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        event_bus: EventBus,
        exchange_service: ExchangeInterface,
        grid_manager: GridManager,
        order_manager: OrderManager,
        balance_tracker: BalanceTracker,
        trading_performance_analyzer: TradingPerformanceAnalyzer,
        trading_pair: str,
    ):
        super().__init__(config_manager, balance_tracker)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.event_bus = event_bus
        self.exchange_service = exchange_service
        self.grid_manager = grid_manager
        self.order_manager = order_manager
        self.trading_performance_analyzer = trading_performance_analyzer
        self.trading_pair = trading_pair
        self._running = True
        self._tick_lock = asyncio.Lock()
        self.armed = False
        self.stop_reason: str | None = None
        trailing_config = config_manager.config.risk_management.trailing_stop_loss
        self.trailing_stop: TrailingStopLoss | None = (
            TrailingStopLoss(trailing_config.atr_multiplier) if trailing_config.enabled else None
        )
        self._bars_since_regrid: int = 0

    def initialize_strategy(self):
        """
        Initializes the trading strategy by setting up the grid and levels.
        This method prepares the strategy to be ready for trading.
        """
        grid_strategy = self.config_manager.config.grid_strategy
        if grid_strategy.dynamic_spacing.enabled and grid_strategy.range is None:
            self.logger.info("Dynamic spacing without static range: grid initialization deferred to ATR warm-up.")
            return
        self.grid_manager.initialize_grids_and_levels()

    async def stop(self):
        """Stops processing prices and ends the ticker feed; the bot owns order cancellation and the exchange close."""
        self._running = False
        await self.exchange_service.close_connection()
        self.logger.info("Trading execution stopped.")

    async def wait_for_tick_to_finish(self, timeout: float) -> None:
        """
        Waits up to `timeout` seconds for a ticker update in progress to finish. Returns at once when called from
        inside that update, which cannot finish before its own stop does.
        """
        if _IN_TICK.get():
            return
        try:
            await asyncio.wait_for(self._tick_lock.acquire(), timeout)
        except TimeoutError:
            self.logger.warning(f"A ticker update was still running after {timeout} s; shutting down without it.")
            return
        self._tick_lock.release()

    async def run(
        self,
        skip_initial_purchase: bool = False,
        skip_grid_init: bool = False,
    ):
        """
        Resets the session state, then runs the trading session until it ends or the bot stops it.

        Args:
            skip_initial_purchase: If True, skip the initial crypto purchase (recovery mode).
            skip_grid_init: If True, skip grid order initialization (recovery mode).

        Raises:
            Exception: If any error occurs during the trading session.
        """
        self._running = True
        self.armed = False
        self.stop_reason = None
        await self._run_session(skip_initial_purchase, skip_grid_init)

    @abstractmethod
    async def _run_session(self, skip_initial_purchase: bool, skip_grid_init: bool) -> None:
        """Runs the trading session on this strategy's price source."""

    async def _initialize_grid_orders_once(
        self,
        current_price: float,
        grid_orders_initialized: bool,
        skip_initial_purchase: bool = False,
    ) -> bool:
        """
        Performs the initial purchase and grid order setup the first time the price
        is inside the grid's price range (at startup or when entering it from either side).

        Returns:
            bool: True if grid orders have been initialized, False otherwise.
        """
        if grid_orders_initialized:
            self.armed = True
            return True

        if not self.grid_manager.is_within_grid_range(current_price):
            self.logger.debug(f"Current price {current_price} is outside the grid range. Waiting.")
            return False

        try:
            self.order_manager.validate_grid_feasibility(current_price)
        except GridFeasibilityError as e:
            self.logger.error(f"Grid infeasible: {e}")
            self.stop_reason = "grid_infeasible"
            await self.event_bus.publish(Events.STOP_BOT, StopRequest(StopKind.INFEASIBLE, f"Grid infeasible: {e}"))
            return False

        if not skip_initial_purchase:
            self.logger.info(
                f"Current price {current_price} is within the grid range. Will perform initial purchase",
            )
            try:
                done = await self.order_manager.perform_initial_purchase(current_price)
            except BookingError as e:
                if self.order_manager.fail_fast:
                    raise
                self.logger.error(f"Initial purchase executed but not booked; halting: {e}")
                self.stop_reason = "booking_error"
                await self.event_bus.publish(
                    Events.STOP_BOT, StopRequest(StopKind.RISK, f"Initial purchase not booked: {e}")
                )
                return False
            if not done:
                self.logger.info("Initial purchase not done yet; the grid waits for it.")
                return False
        else:
            self.logger.info("Skipping initial purchase (recovered from persisted state).")
        self.logger.info("Initial purchase done, will initialize grid orders")
        await self.order_manager.initialize_grid_orders(current_price)
        self.armed = True
        return True

    def generate_performance_report(self) -> tuple[PerformanceMetrics | None, dict, list]:
        """
        Generates a performance report for the trading session.

        It evaluates the strategy's performance by analyzing
        the account value, fees, and final price over the given timeframe.

        Returns:
            tuple: The typed metrics (None without data), their display summary and the formatted orders.
        """
        report_data = self._report_data()
        if report_data is None:
            return None, {}, []
        return self._summary(*report_data)

    @abstractmethod
    def _report_data(self) -> tuple[pd.DataFrame, float, float, float] | None:
        """
        Returns the account values, the initial and final prices and the initial balance the report is built from,
        or None (after a warning) when the session has none.
        """

    def _summary(
        self, data: pd.DataFrame, initial_price: float, final_price: float, initial_balance: float
    ) -> tuple[PerformanceMetrics | None, dict, list]:
        """Builds the performance summary from the session's account values and the tracker's balances and PnL."""
        snapshot = self.balance_tracker.snapshot(final_price)
        return self.trading_performance_analyzer.generate_performance_summary(
            data,
            initial_price,
            self.balance_tracker.get_adjusted_fiat_balance(),
            self.balance_tracker.get_adjusted_crypto_balance(),
            final_price,
            self.balance_tracker.total_fees,
            snapshot.realized_pnl,
            snapshot.unrealized_pnl,
            initial_balance=initial_balance,
            armed=self.armed,
            stop_reason=self.stop_reason,
        )

    async def _handle_take_profit_stop_loss(self, current_price: float) -> bool:
        """
        Handles take-profit or stop-loss events based on the current price.
        Publishes a STOP_BOT event if either condition is triggered.
        """
        tp_or_sl_triggered = await self._evaluate_tp_or_sl(current_price)
        if tp_or_sl_triggered:
            self.logger.info("Take-profit or stop-loss triggered, ending trading session.")
            await self.event_bus.publish(Events.STOP_BOT, StopRequest(StopKind.RISK, "TP or SL hit."))
            return True
        return False

    async def _handle_trailing_stop(self, current_price: float, atr: float) -> bool:
        """
        Updates and evaluates the ATR-based trailing stop. Returns True when trading must stop.
        """
        if self.trailing_stop is None or self.balance_tracker.get_adjusted_crypto_balance() == 0:
            return False

        self.trailing_stop.update(current_price, atr)

        # With NaN/invalid ATR the ratchet cannot move; do not evaluate the trigger.
        # After a live restart a restored stop_price could otherwise fire during the
        # pre-first-candle window (ATR still NaN), and the regrid branch would no-op
        # yet reset(), discarding the restored protection.
        if math.isnan(atr) or atr <= 0:
            return False

        if not self.trailing_stop.is_triggered(current_price):
            return False

        if self.config_manager.config.risk_management.trailing_stop_loss.on_trigger is TrailingTrigger.STOP:
            self.logger.info(f"Trailing stop triggered at {current_price}. Liquidating and stopping.")
            self.stop_reason = "trailing_stop"
            await self.order_manager.execute_take_profit_or_stop_loss_order(
                current_price=current_price,
                stop_loss_order=True,
            )
            await self.event_bus.publish(Events.STOP_BOT, StopRequest(StopKind.RISK, "Trailing stop hit."))
            return True

        self.logger.info(f"Trailing stop triggered at {current_price}. Regridding around current price.")
        if await self._execute_regrid(current_price, atr):
            self.trailing_stop.reset()
        return False

    async def _maybe_regrid_on_volatility(self, current_price: float, atr: float) -> None:
        """
        Rebuilds the grid when the ATR regime has drifted beyond the configured threshold,
        subject to a cooldown period between regrids.
        """
        dynamic_spacing = self.config_manager.config.grid_strategy.dynamic_spacing
        if not dynamic_spacing.enabled or not self.grid_manager.is_initialized:
            return
        if math.isnan(atr) or atr <= 0:
            return

        self._bars_since_regrid += 1

        if self.grid_manager.atr_grid is None or self.grid_manager.atr_grid <= 0:
            self.grid_manager.atr_grid = atr  # baseline for grids built from a static range
            return
        if self._bars_since_regrid < dynamic_spacing.cooldown_bars:
            return
        if abs(atr / self.grid_manager.atr_grid - 1) <= dynamic_spacing.regrid_threshold:
            return

        self.logger.info(
            f"Volatility regime change (ATR {self.grid_manager.atr_grid} -> {atr}). Regridding.",
        )
        await self._execute_regrid(current_price, atr)

    async def _execute_regrid(self, center_price: float, atr: float) -> bool:
        """
        Rebuilds the grid around center_price with the given ATR through OrderManager.regrid, which cancels and
        re-places the orders under one lock.

        Returns:
            bool: True only when the grid was rebuilt and orders re-placed on the NEW grid;
            False on the NaN/invalid-ATR guard, a cancel failure, or a rejected regrid.
        """
        if math.isnan(atr) or atr <= 0:
            return False

        self._bars_since_regrid = 0
        return await self.order_manager.regrid(center_price, lambda: self.grid_manager.regrid(center_price, atr))

    def export_strategy_state(self) -> dict:
        """
        Returns the trailing stop ratchet, ATR grid baseline, and current grid geometry
        for crash-recovery checkpoints. The grid geometry (``price_grids``/``central_price``)
        is included only once the grid is initialized so a runtime regrid can be restored
        exactly on restart; otherwise those keys are None.
        """
        initialized = self.grid_manager.is_initialized
        return {
            "trailing_stop": self.trailing_stop.to_dict() if self.trailing_stop else None,
            "atr_grid": self.grid_manager.atr_grid,
            "price_grids": list(self.grid_manager.price_grids) if initialized else None,
            "central_price": self.grid_manager.central_price if initialized else None,
        }

    def restore_strategy_state(self, state: dict) -> None:
        """Restores the trailing stop ratchet and ATR grid baseline recovered from persisted state."""
        trailing = state.get("trailing_stop")
        trailing_config = self.config_manager.config.risk_management.trailing_stop_loss
        if trailing is not None and trailing_config.enabled:
            self.trailing_stop = TrailingStopLoss.from_dict(trailing)
            self.trailing_stop.atr_multiplier = trailing_config.atr_multiplier

        atr_grid = state.get("atr_grid")
        if atr_grid is not None:
            self.grid_manager.atr_grid = atr_grid

        self.logger.info("Restored strategy state (trailing stop / ATR grid baseline).")

    async def _evaluate_tp_or_sl(self, current_price: float) -> bool:
        """
        Evaluates whether take-profit or stop-loss conditions are met.
        Returns True if any condition is triggered.
        """
        if self.balance_tracker.get_adjusted_crypto_balance() == 0:
            self.logger.debug("No crypto held; skipping TP/SL checks.")
            return False

        return await self._handle_take_profit(current_price) or await self._handle_stop_loss(current_price)

    async def _handle_take_profit(self, current_price: float) -> bool:
        """
        Handles take-profit logic and executes a TP order if conditions are met.
        Returns True if take-profit is triggered.
        """
        take_profit = self.config_manager.config.risk_management.take_profit
        if take_profit.enabled and current_price >= take_profit.threshold:
            self.logger.info(f"Take-profit triggered at {current_price}. Executing TP order...")
            self.stop_reason = "take_profit"
            await self.order_manager.execute_take_profit_or_stop_loss_order(
                current_price=current_price,
                take_profit_order=True,
            )
            return True
        return False

    async def _handle_stop_loss(self, current_price: float) -> bool:
        """
        Handles stop-loss logic and executes an SL order if conditions are met.
        Returns True if stop-loss is triggered.
        """
        stop_loss = self.config_manager.config.risk_management.stop_loss
        if stop_loss.enabled and current_price <= stop_loss.threshold:
            self.logger.info(f"Stop-loss triggered at {current_price}. Executing SL order...")
            self.stop_reason = "stop_loss"
            await self.order_manager.execute_take_profit_or_stop_loss_order(
                current_price=current_price,
                stop_loss_order=True,
            )
            return True
        return False

    def get_formatted_orders(self):
        """
        Retrieves a formatted summary of all orders.

        Returns:
            list: A list of formatted orders.
        """
        return self.trading_performance_analyzer.get_formatted_orders()
