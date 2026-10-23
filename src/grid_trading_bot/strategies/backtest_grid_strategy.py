import asyncio
import math

import numpy as np
import pandas as pd

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.indicators.atr_calculator import ATRCalculator
from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator
from grid_trading_bot.core.services.exceptions import DataFetchError, HistoricalMarketDataFileNotFoundError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.strategies.plotter import Plotter
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer

from .grid_trading_strategy import GridTradingStrategy


class BacktestGridStrategy(GridTradingStrategy):
    """Runs the grid over historical candles, filling orders through the order simulator, and plots the result."""

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
        plotter: Plotter | None,
        order_simulator: OrderSimulator,
        backtest_clock: BacktestClock,
    ):
        super().__init__(
            config_manager,
            event_bus,
            exchange_service,
            grid_manager,
            order_manager,
            balance_tracker,
            trading_performance_analyzer,
            trading_pair,
        )
        self.plotter = plotter
        self.order_simulator = order_simulator
        self.backtest_clock = backtest_clock
        self.data: pd.DataFrame | None = None
        self.close_prices: np.ndarray | None = None
        self._starting_account_value: float | None = None
        self._trailing_atr_series: pd.Series | None = None
        self._dynamic_atr_series: pd.Series | None = None

    async def _run_session(self, skip_initial_purchase: bool, skip_grid_init: bool) -> None:
        """Loads the candles, precomputes the ATR series and simulates the grid over every candle."""
        self.data = await self._initialize_historical_data()
        self._precompute_backtest_atr()
        start_index = self._initialize_dynamic_grid_backtest()
        await self._run_backtest(start_index)
        self.logger.info("Ending backtest simulation")
        self._running = False

    async def _initialize_historical_data(self) -> pd.DataFrame:
        """
        Loads the historical market data (OHLCV) of the configured period; a load failure is logged and raised.
        Uses asyncio.to_thread to avoid blocking the event loop during I/O.
        """
        try:
            timeframe, start_date, end_date = self._extract_config()
            return await asyncio.to_thread(
                self.exchange_service.fetch_ohlcv, self.trading_pair, timeframe, start_date, end_date
            )
        except (DataFetchError, HistoricalMarketDataFileNotFoundError) as e:
            self.logger.error(f"Failed to initialize data for backtest trading mode: {e}")
            raise

    def _extract_config(self) -> tuple[str, str, str]:
        """
        Extracts configuration values for timeframe, start date, and end date.

        Returns:
            tuple: A tuple containing the timeframe, start date, and end date as strings.
        """
        settings = self.config_manager.config.trading_settings
        return settings.timeframe, settings.period.start_date, settings.period.end_date

    async def _run_backtest(self, start_index: int = 0) -> None:
        """
        Executes the backtesting simulation based on historical OHLCV data.

        This method simulates trading using preloaded data, managing grid levels,
        executing orders, and updating account values over the timeframe.

        Args:
            start_index (int): Candle index at which trading may start (ATR warm-up for dynamic grids).
        """
        if self.data is None:
            self.logger.error("No data available for backtesting.")
            return

        self.logger.info("Starting backtest simulation")
        close_prices = self.data["close"].to_numpy()
        self.close_prices = close_prices
        high_prices = self.data["high"].to_numpy()
        low_prices = self.data["low"].to_numpy()
        timestamps = self.data.index
        account_values = np.full(len(self.data), np.nan)
        crypto_values = np.full(len(self.data), np.nan)
        grid_orders_initialized = False
        self._starting_account_value = self.balance_tracker.get_total_balance_value(close_prices[0])

        for i, (current_price, high_price, low_price, timestamp) in enumerate(
            zip(close_prices, high_prices, low_prices, timestamps, strict=False),
        ):
            self.backtest_clock.set(timestamp)
            if i >= start_index and self.stop_reason is None:
                if not grid_orders_initialized:
                    # The grid is placed at this candle's close, so its high and low are already in the past.
                    grid_orders_initialized = await self._initialize_grid_orders_once(current_price, False)
                else:
                    await self._simulate_backtest_candle(i, current_price, high_price, low_price, timestamp)

            account_values[i] = self.balance_tracker.get_total_balance_value(current_price)
            crypto_values[i] = self.balance_tracker.get_adjusted_crypto_balance() * current_price

        self.data["account_value"] = account_values
        self.data["crypto_value"] = crypto_values

    async def _simulate_backtest_candle(
        self,
        index: int,
        current_price: float,
        high_price: float,
        low_price: float,
        timestamp: pd.Timestamp,
    ) -> None:
        """
        Fills the orders the candle crosses, then applies the trailing stop, TP/SL and the
        volatility regrid at its close. A stop sets `stop_reason`, which ends trading.
        """
        await self.order_simulator.simulate_order_fills(high_price, low_price, timestamp)

        trailing_atr = self._backtest_atr_at(self._trailing_atr_series, index)
        if await self._handle_trailing_stop(current_price, trailing_atr):
            return

        if await self._handle_take_profit_stop_loss(current_price):
            return

        dynamic_atr = self._backtest_atr_at(self._dynamic_atr_series, index)
        await self._maybe_regrid_on_volatility(current_price, dynamic_atr)

    def _precompute_backtest_atr(self) -> None:
        """
        Precomputes ATR series for the trailing stop and dynamic spacing features, aligned on self.data.
        """
        if self.data is None:
            return
        trailing_config = self.config_manager.config.risk_management.trailing_stop_loss
        if trailing_config.enabled:
            self._trailing_atr_series = ATRCalculator.compute_series(self.data, trailing_config.atr_period)
        dynamic_spacing = self.config_manager.config.grid_strategy.dynamic_spacing
        if dynamic_spacing.enabled:
            self._dynamic_atr_series = ATRCalculator.compute_series(self.data, dynamic_spacing.atr_period)

    def _backtest_atr_at(self, series: pd.Series | None, index: int) -> float:
        """Returns the ATR value at the given candle index, or NaN if the series is disabled."""
        if series is None:
            return math.nan
        return float(series.iloc[index])

    def _initialize_dynamic_grid_backtest(self) -> int:
        """
        When dynamic spacing is on and the grid was not built from a static range,
        builds the initial grid from the first available ATR.
        Returns the candle index at which the trading loop may start.
        """
        dynamic_spacing = self.config_manager.config.grid_strategy.dynamic_spacing
        if not dynamic_spacing.enabled or self.grid_manager.is_initialized:
            return 0

        period = dynamic_spacing.atr_period
        warmup_index = period + 1
        if self.data is None or len(self.data) <= warmup_index:
            raise ValueError("Not enough candles to warm up ATR for dynamic grid initialization.")

        atr = self._backtest_atr_at(self._dynamic_atr_series, warmup_index)
        center = float(self.data["close"].iloc[warmup_index])
        self.grid_manager.regrid(center, atr)
        self.logger.info(f"Initial dynamic grid built at candle {warmup_index} (center {center}, ATR {atr}).")
        return warmup_index

    def _report_data(self) -> tuple[pd.DataFrame, float, float, float] | None:
        if self.data is None or self.close_prices is None or self._starting_account_value is None:
            self.logger.warning("No backtest data; no performance report.")
            return None
        return self.data, self.close_prices[0], self.close_prices[-1], self._starting_account_value

    def plot_results(self) -> None:
        """Plots the backtest results using the provided plotter."""
        if self.data is None or self.plotter is None:
            self.logger.warning("No backtest data to plot.")
            return
        self.plotter.plot_results(self.data)
