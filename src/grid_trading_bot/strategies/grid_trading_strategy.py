import asyncio
from collections import deque
import logging
import math

import pandas as pd

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.indicators.atr_calculator import ATRCalculator
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator
from grid_trading_bot.core.risk_management.trailing_stop_loss import TrailingStopLoss
from grid_trading_bot.core.services.exceptions import DataFetchError, HistoricalMarketDataFileNotFoundError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.strategies.plotter import Plotter
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer

from .trading_strategy_interface import TradingStrategyInterface


class GridTradingStrategy(TradingStrategyInterface):
    TICKER_REFRESH_INTERVAL = 3  # in seconds
    MAX_LIVE_METRICS = 86_400  # 24h at ~1 data point per 3 seconds

    def __init__(
        self,
        config_manager: ConfigManager,
        event_bus: EventBus,
        exchange_service: ExchangeInterface,
        grid_manager: GridManager,
        order_manager: OrderManager,
        balance_tracker: BalanceTracker,
        trading_performance_analyzer: TradingPerformanceAnalyzer,
        trading_mode: TradingMode,
        trading_pair: str,
        plotter: Plotter | None = None,
        order_simulator: OrderSimulator | None = None,
    ):
        super().__init__(config_manager, balance_tracker)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.event_bus = event_bus
        self.exchange_service = exchange_service
        self.grid_manager = grid_manager
        self.order_manager = order_manager
        self.order_simulator = order_simulator
        self.trading_performance_analyzer = trading_performance_analyzer
        self.trading_mode = trading_mode
        self.trading_pair = trading_pair
        self.plotter = plotter
        self.data: pd.DataFrame | None = None
        self.close_prices = None
        self.live_trading_metrics: deque = deque(maxlen=self.MAX_LIVE_METRICS)
        self._running = True
        self.trailing_stop: TrailingStopLoss | None = (
            TrailingStopLoss(config_manager.get_trailing_atr_multiplier())
            if config_manager.is_trailing_stop_loss_enabled()
            else None
        )
        self._bars_since_regrid: int = 0
        self._trailing_atr_series: pd.Series | None = None
        self._dynamic_atr_series: pd.Series | None = None
        self._live_atr: dict[int, float] = {}  # period -> latest ATR (live mode, Task 8)
        self._next_candle_close: pd.Timestamp | None = None

    async def _initialize_historical_data(self) -> pd.DataFrame | None:
        """
        Initializes historical market data (OHLCV).
        In LIVE or PAPER_TRADING mode returns None.
        Uses asyncio.to_thread to avoid blocking the event loop during I/O.
        """
        if self.trading_mode != TradingMode.BACKTEST:
            return None

        try:
            timeframe, start_date, end_date = self._extract_config()
            return await asyncio.to_thread(
                self.exchange_service.fetch_ohlcv, self.trading_pair, timeframe, start_date, end_date
            )
        except (DataFetchError, HistoricalMarketDataFileNotFoundError) as e:
            self.logger.error(f"Failed to initialize data for backtest trading mode: {e}")
            return None

    def _extract_config(self) -> tuple[str, str, str]:
        """
        Extracts configuration values for timeframe, start date, and end date.

        Returns:
            tuple: A tuple containing the timeframe, start date, and end date as strings.
        """
        timeframe = self.config_manager.get_timeframe()
        start_date = self.config_manager.get_start_date()
        end_date = self.config_manager.get_end_date()
        return timeframe, start_date, end_date

    def initialize_strategy(self):
        """
        Initializes the trading strategy by setting up the grid and levels.
        This method prepares the strategy to be ready for trading.
        """
        if self.config_manager.is_dynamic_spacing_enabled() and self.config_manager.get_top_range() is None:
            self.logger.info("Dynamic spacing without static range: grid initialization deferred to ATR warm-up.")
            return
        self.grid_manager.initialize_grids_and_levels()

    async def stop(self):
        """
        Stops the trading execution.

        This method halts all trading activities, closes active exchange
        connections, and updates the internal state to indicate the bot
        is no longer running.
        """
        self._running = False
        await self.exchange_service.close_connection()
        self.logger.info("Trading execution stopped.")

    async def restart(self):
        """
        Restarts the trading session. If the strategy is not running, starts it.
        """
        if not self._running:
            self.logger.info("Restarting trading session.")
            await self.run()

    async def run(
        self,
        skip_initial_purchase: bool = False,
        skip_grid_init: bool = False,
    ):
        """
        Starts the trading session based on the configured mode.

        For backtesting, this simulates the strategy using historical data.
        For live or paper trading, this interacts with the exchange to manage
        real-time trading.

        Args:
            skip_initial_purchase: If True, skip the initial crypto purchase (recovery mode).
            skip_grid_init: If True, skip grid order initialization (recovery mode).

        Raises:
            Exception: If any error occurs during the trading session.
        """
        self._running = True
        self.data = await self._initialize_historical_data()
        trigger_price = self.grid_manager.get_trigger_price() if self.grid_manager.is_initialized else None

        if self.trading_mode == TradingMode.BACKTEST:
            self._precompute_backtest_atr()
            start_index = self._initialize_dynamic_grid_backtest()
            if trigger_price is None:
                trigger_price = self.grid_manager.get_trigger_price()
            await self._run_backtest(trigger_price, start_index)
            self.logger.info("Ending backtest simulation")
            self._running = False
        else:
            await self._run_live_or_paper_trading(
                trigger_price,
                skip_initial_purchase=skip_initial_purchase,
                skip_grid_init=skip_grid_init,
            )

    async def _run_live_or_paper_trading(
        self,
        trigger_price: float,
        skip_initial_purchase: bool = False,
        skip_grid_init: bool = False,
    ):
        """
        Executes live or paper trading sessions based on real-time ticker updates.

        The method listens for ticker updates, initializes grid orders when
        the trigger price is reached, and manages take-profit and stop-loss events.

        Args:
            trigger_price (float): The price at which grid orders are triggered.
            skip_initial_purchase: If True, skip initial purchase on grid init (recovery).
            skip_grid_init: If True, mark grid orders as already initialized (recovery).
        """
        self.logger.info(f"Starting {'live' if self.trading_mode == TradingMode.LIVE else 'paper'} trading")

        if self.config_manager.is_dynamic_spacing_enabled() and not self.grid_manager.is_initialized:
            timeframe = self.config_manager.get_timeframe()
            period = self.config_manager.get_dynamic_atr_period()
            candles = await self.exchange_service.fetch_recent_ohlcv(self.trading_pair, timeframe, period * 3)
            atr = ATRCalculator.compute(candles, period)
            current_price = await self.exchange_service.get_current_price(self.trading_pair)
            self.grid_manager.regrid(current_price, atr)
            trigger_price = self.grid_manager.get_trigger_price()
            self.logger.info(f"Initial dynamic grid built live (center {current_price}, ATR {atr}).")

        last_price: float | None = None
        grid_orders_initialized = skip_grid_init

        async def on_ticker_update(current_price):
            nonlocal last_price, grid_orders_initialized
            try:
                if not self._running:
                    self.logger.info("Trading stopped; halting price updates.")
                    return

                account_value = self.balance_tracker.get_total_balance_value(current_price)
                self.live_trading_metrics.append((pd.Timestamp.now(), account_value, current_price))

                grid_orders_initialized = await self._initialize_grid_orders_once(
                    current_price,
                    trigger_price,
                    grid_orders_initialized,
                    last_price,
                    skip_initial_purchase=skip_initial_purchase,
                )

                if not grid_orders_initialized:
                    last_price = current_price
                    return

                await self._maybe_refresh_live_atr(pd.Timestamp.now())

                trailing_atr = self._live_atr.get(
                    self.config_manager.get_trailing_atr_period(),
                    math.nan,
                )
                if await self._handle_trailing_stop(current_price, trailing_atr):
                    return

                if await self._handle_take_profit_stop_loss(current_price):
                    return

                dynamic_atr = self._live_atr.get(
                    self.config_manager.get_dynamic_atr_period(),
                    math.nan,
                )
                await self._maybe_regrid_on_volatility(current_price, dynamic_atr)

                last_price = current_price

            except Exception as e:
                self.logger.error(f"Error during ticker update: {e}", exc_info=True)

        try:
            await self.exchange_service.listen_to_ticker_updates(
                self.trading_pair,
                on_ticker_update,
                self.TICKER_REFRESH_INTERVAL,
            )

        except Exception as e:
            self.logger.error(f"Error in live/paper trading loop: {e}", exc_info=True)

        finally:
            self.logger.info("Exiting live/paper trading loop.")

    async def _run_backtest(self, trigger_price: float, start_index: int = 0) -> None:
        """
        Executes the backtesting simulation based on historical OHLCV data.

        This method simulates trading using preloaded data, managing grid levels,
        executing orders, and updating account values over the timeframe.

        Args:
            trigger_price (float): The price at which grid orders are triggered.
            start_index (int): Candle index at which trading may start (ATR warm-up for dynamic grids).
        """
        if self.data is None:
            self.logger.error("No data available for backtesting.")
            return

        self.logger.info("Starting backtest simulation")
        self.data["account_value"] = math.nan
        self.close_prices = self.data["close"].values
        high_prices = self.data["high"].values
        low_prices = self.data["low"].values
        timestamps = self.data.index
        self.data.loc[timestamps[0], "account_value"] = self.balance_tracker.get_total_balance_value(
            price=self.close_prices[0],
        )
        grid_orders_initialized = False
        last_price = None

        for i, (current_price, high_price, low_price, timestamp) in enumerate(
            zip(self.close_prices, high_prices, low_prices, timestamps, strict=False),
        ):
            if i < start_index:
                self.data.loc[timestamps[i], "account_value"] = self.balance_tracker.get_total_balance_value(
                    price=current_price,
                )
                last_price = current_price
                continue

            grid_orders_initialized = await self._initialize_grid_orders_once(
                current_price,
                trigger_price,
                grid_orders_initialized,
                last_price,
            )

            if not grid_orders_initialized:
                self.data.loc[timestamps[i], "account_value"] = self.balance_tracker.get_total_balance_value(
                    price=current_price,
                )
                last_price = current_price
                continue

            await self.order_simulator.simulate_order_fills(high_price, low_price, timestamp)

            trailing_atr = self._backtest_atr_at(self._trailing_atr_series, i)
            if await self._handle_trailing_stop(current_price, trailing_atr):
                break

            if await self._handle_take_profit_stop_loss(current_price):
                break

            dynamic_atr = self._backtest_atr_at(self._dynamic_atr_series, i)
            await self._maybe_regrid_on_volatility(current_price, dynamic_atr)

            self.data.loc[timestamp, "account_value"] = self.balance_tracker.get_total_balance_value(current_price)
            last_price = current_price

    async def _initialize_grid_orders_once(
        self,
        current_price: float,
        trigger_price: float,
        grid_orders_initialized: bool,
        last_price: float | None = None,
        skip_initial_purchase: bool = False,
    ) -> bool:
        """
        Performs the initial purchase and grid order setup when the trigger price is first crossed.

        Returns:
            bool: True if grid orders have been initialized, False otherwise.
        """
        if grid_orders_initialized:
            return True

        if last_price is None:
            self.logger.debug("No previous price recorded yet. Waiting for the next price update.")
            return False

        if last_price <= trigger_price <= current_price or last_price == trigger_price:
            if not skip_initial_purchase:
                self.logger.info(
                    f"Current price {current_price} reached trigger price {trigger_price}. "
                    f"Will perform initial purchase",
                )
                await self.order_manager.perform_initial_purchase(current_price)
                await self.event_bus.publish(Events.INITIAL_PURCHASE_DONE, None)
            else:
                self.logger.info("Skipping initial purchase (recovered from persisted state).")
            self.logger.info("Initial purchase done, will initialize grid orders")
            await self.order_manager.initialize_grid_orders(current_price)
            await self.event_bus.publish(Events.GRID_ORDERS_INITIALIZED, None)
            return True

        self.logger.debug(
            f"Current price {current_price} did not cross trigger price {trigger_price}. Last price: {last_price}.",
        )
        return False

    def generate_performance_report(self) -> tuple[dict, list]:
        """
        Generates a performance report for the trading session.

        It evaluates the strategy's performance by analyzing
        the account value, fees, and final price over the given timeframe.

        Returns:
            tuple: A dictionary summarizing performance metrics and a list of formatted order details.
        """
        if self.trading_mode == TradingMode.BACKTEST:
            initial_price = self.close_prices[0]
            final_price = self.close_prices[-1]
            return self.trading_performance_analyzer.generate_performance_summary(
                self.data,
                initial_price,
                self.balance_tracker.get_adjusted_fiat_balance(),
                self.balance_tracker.get_adjusted_crypto_balance(),
                final_price,
                self.balance_tracker.total_fees,
            )
        else:
            if not self.live_trading_metrics:
                self.logger.warning("No account value data available for live/paper trading mode.")
                return {}, []

            live_data = pd.DataFrame(self.live_trading_metrics, columns=["timestamp", "account_value", "price"])
            live_data.set_index("timestamp", inplace=True)
            initial_price = live_data.iloc[0]["price"]
            final_price = live_data.iloc[-1]["price"]

            return self.trading_performance_analyzer.generate_performance_summary(
                live_data,
                initial_price,
                self.balance_tracker.get_adjusted_fiat_balance(),
                self.balance_tracker.get_adjusted_crypto_balance(),
                final_price,
                self.balance_tracker.total_fees,
            )

    def plot_results(self) -> None:
        """
        Plots the backtest results using the provided plotter.

        This method generates and displays visualizations of the trading
        strategy's performance during backtesting. If the bot is running
        in live or paper trading mode, plotting is not available.
        """
        if self.trading_mode == TradingMode.BACKTEST:
            self.plotter.plot_results(self.data)
        else:
            self.logger.info("Plotting is not available for live/paper trading mode.")

    async def _handle_take_profit_stop_loss(self, current_price: float) -> bool:
        """
        Handles take-profit or stop-loss events based on the current price.
        Publishes a STOP_BOT event if either condition is triggered.
        """
        tp_or_sl_triggered = await self._evaluate_tp_or_sl(current_price)
        if tp_or_sl_triggered:
            self.logger.info("Take-profit or stop-loss triggered, ending trading session.")
            await self.event_bus.publish(Events.STOP_BOT, "TP or SL hit.")
            return True
        return False

    async def _handle_trailing_stop(self, current_price: float, atr: float) -> bool:
        """
        Updates and evaluates the ATR-based trailing stop. Returns True when trading must stop.
        """
        if self.trailing_stop is None or self.balance_tracker.crypto_balance == 0:
            return False

        self.trailing_stop.update(current_price, atr)
        if not self.trailing_stop.is_triggered(current_price):
            return False

        if self.config_manager.get_trailing_on_trigger() == "stop":
            self.logger.info(f"Trailing stop triggered at {current_price}. Liquidating and stopping.")
            await self.order_manager.execute_take_profit_or_stop_loss_order(
                current_price=current_price,
                stop_loss_order=True,
            )
            await self.event_bus.publish(Events.STOP_BOT, "Trailing stop hit.")
            return True

        self.logger.info(f"Trailing stop triggered at {current_price}. Regridding around current price.")
        await self._execute_regrid(current_price, atr)
        self.trailing_stop.reset()
        return False

    async def _maybe_regrid_on_volatility(self, current_price: float, atr: float) -> None:
        """
        Rebuilds the grid when the ATR regime has drifted beyond the configured threshold,
        subject to a cooldown period between regrids.
        """
        if not self.config_manager.is_dynamic_spacing_enabled() or not self.grid_manager.is_initialized:
            return
        if math.isnan(atr) or atr <= 0:
            return

        self._bars_since_regrid += 1

        if self.grid_manager.atr_grid is None or self.grid_manager.atr_grid <= 0:
            self.grid_manager.atr_grid = atr  # baseline for grids built from a static range
            return
        if self._bars_since_regrid < self.config_manager.get_cooldown_bars():
            return
        if abs(atr / self.grid_manager.atr_grid - 1) <= self.config_manager.get_regrid_threshold():
            return

        self.logger.info(
            f"Volatility regime change (ATR {self.grid_manager.atr_grid} -> {atr}). Regridding.",
        )
        await self._execute_regrid(current_price, atr)

    async def _execute_regrid(self, center_price: float, atr: float) -> None:
        """
        Cancels open grid orders and rebuilds the grid around center_price with the given ATR.
        Falls back to re-placing orders on the existing grid if cancellation or regrid fails.
        """
        if math.isnan(atr) or atr <= 0:
            return

        cancelled = await self.order_manager.cancel_open_grid_orders()
        self._bars_since_regrid = 0
        if not cancelled:
            self.logger.warning("Regrid aborted: could not cancel all open orders. Re-placing cancelled ones.")
            await self.order_manager.initialize_grid_orders(center_price)
            return

        try:
            self.grid_manager.regrid(center_price, atr)
        except ValueError as e:
            self.logger.warning(f"Regrid rejected ({e}). Re-placing orders on the existing grid.")
            await self.order_manager.initialize_grid_orders(center_price)
            return

        await self.order_manager.initialize_grid_orders(center_price)

    def export_strategy_state(self) -> dict:
        """Returns the trailing stop ratchet and ATR grid baseline for crash-recovery checkpoints."""
        return {
            "trailing_stop": self.trailing_stop.to_dict() if self.trailing_stop else None,
            "atr_grid": self.grid_manager.atr_grid,
        }

    def restore_strategy_state(self, state: dict) -> None:
        """Restores the trailing stop ratchet and ATR grid baseline recovered from persisted state."""
        trailing = state.get("trailing_stop")
        if trailing is not None and self.config_manager.is_trailing_stop_loss_enabled():
            self.trailing_stop = TrailingStopLoss.from_dict(trailing)

        atr_grid = state.get("atr_grid")
        if atr_grid is not None:
            self.grid_manager.atr_grid = atr_grid

        self.logger.info("Restored strategy state (trailing stop / ATR grid baseline).")

    def _precompute_backtest_atr(self) -> None:
        """
        Precomputes ATR series for the trailing stop and dynamic spacing features, aligned on self.data.
        """
        if self.data is None:
            return
        if self.config_manager.is_trailing_stop_loss_enabled():
            self._trailing_atr_series = ATRCalculator.compute_series(
                self.data,
                self.config_manager.get_trailing_atr_period(),
            )
        if self.config_manager.is_dynamic_spacing_enabled():
            self._dynamic_atr_series = ATRCalculator.compute_series(
                self.data,
                self.config_manager.get_dynamic_atr_period(),
            )

    def _backtest_atr_at(self, series: pd.Series | None, index: int) -> float:
        """Returns the ATR value at the given candle index, or NaN if the series is disabled."""
        if series is None:
            return math.nan
        return float(series.iloc[index])

    @staticmethod
    def _timeframe_to_seconds(timeframe: str) -> int:
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        return int(timeframe[:-1]) * units[timeframe[-1]]

    def _enabled_atr_periods(self) -> set[int]:
        periods: set[int] = set()
        if self.config_manager.is_trailing_stop_loss_enabled():
            periods.add(self.config_manager.get_trailing_atr_period())
        if self.config_manager.is_dynamic_spacing_enabled():
            periods.add(self.config_manager.get_dynamic_atr_period())
        return periods

    async def _maybe_refresh_live_atr(self, now: pd.Timestamp) -> None:
        """
        Fetches recent candles and refreshes self._live_atr for every enabled ATR-based
        feature once a candle boundary has passed. No-op if no such feature is enabled.
        """
        periods = self._enabled_atr_periods()
        if not periods:
            return

        timeframe = self.config_manager.get_timeframe()
        interval = pd.Timedelta(seconds=self._timeframe_to_seconds(timeframe))
        if self._next_candle_close is None:
            self._next_candle_close = now.ceil(interval)
            return
        if now < self._next_candle_close:
            return

        self._next_candle_close = self._next_candle_close + interval
        limit = max(periods) * 3  # enough history for Wilder smoothing to stabilize
        try:
            candles = await self.exchange_service.fetch_recent_ohlcv(self.trading_pair, timeframe, limit)
        except DataFetchError as e:
            self.logger.warning(f"ATR refresh failed, keeping previous ATR: {e}")
            return

        for period in periods:
            self._live_atr[period] = ATRCalculator.compute(candles, period)

    def _initialize_dynamic_grid_backtest(self) -> int:
        """
        When dynamic spacing is on and the grid was not built from a static range,
        builds the initial grid from the first available ATR.
        Returns the candle index at which the trading loop may start.
        """
        if not self.config_manager.is_dynamic_spacing_enabled() or self.grid_manager.is_initialized:
            return 0

        period = self.config_manager.get_dynamic_atr_period()
        warmup_index = period + 1
        if self.data is None or len(self.data) <= warmup_index:
            raise ValueError("Not enough candles to warm up ATR for dynamic grid initialization.")

        atr = self._backtest_atr_at(self._dynamic_atr_series, warmup_index)
        center = float(self.data["close"].iloc[warmup_index])
        self.grid_manager.regrid(center, atr)
        self.logger.info(f"Initial dynamic grid built at candle {warmup_index} (center {center}, ATR {atr}).")
        return warmup_index

    async def _evaluate_tp_or_sl(self, current_price: float) -> bool:
        """
        Evaluates whether take-profit or stop-loss conditions are met.
        Returns True if any condition is triggered.
        """
        if self.balance_tracker.crypto_balance == 0:
            self.logger.debug("No crypto balance available; skipping TP/SL checks.")
            return False

        return await self._handle_take_profit(current_price) or await self._handle_stop_loss(current_price)

    async def _handle_take_profit(self, current_price: float) -> bool:
        """
        Handles take-profit logic and executes a TP order if conditions are met.
        Returns True if take-profit is triggered.
        """
        if (
            self.config_manager.is_take_profit_enabled()
            and current_price >= self.config_manager.get_take_profit_threshold()
        ):
            self.logger.info(f"Take-profit triggered at {current_price}. Executing TP order...")
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
        if (
            self.config_manager.is_stop_loss_enabled()
            and current_price <= self.config_manager.get_stop_loss_threshold()
        ):
            self.logger.info(f"Stop-loss triggered at {current_price}. Executing SL order...")
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
