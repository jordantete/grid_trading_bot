from collections import deque
import math

import pandas as pd

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.indicators.atr_calculator import ATRCalculator
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer

from .grid_trading_strategy import _IN_TICK, GridTradingStrategy


class LiveGridStrategy(GridTradingStrategy):
    """Runs the grid on the exchange's ticker feed, refreshing the ATR at each candle close; it has no plot."""

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
        trading_pair: str,
        mode_label: str,
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
        self.mode_label = mode_label
        self.live_trading_metrics: deque = deque(maxlen=self.MAX_LIVE_METRICS)
        self._live_atr: dict[int, float] = {}  # period -> latest ATR
        self._next_candle_close: pd.Timestamp | None = None

    async def _run_session(self, skip_initial_purchase: bool, skip_grid_init: bool) -> None:
        await self._run_live_or_paper_trading(
            skip_initial_purchase=skip_initial_purchase,
            skip_grid_init=skip_grid_init,
        )

    async def _run_live_or_paper_trading(
        self,
        skip_initial_purchase: bool = False,
        skip_grid_init: bool = False,
    ):
        """
        Executes live or paper trading sessions based on real-time ticker updates.

        The method listens for ticker updates, initializes grid orders when
        the price is inside the grid range, and manages take-profit and stop-loss events.

        Args:
            skip_initial_purchase: If True, skip initial purchase on grid init (recovery).
            skip_grid_init: If True, mark grid orders as already initialized (recovery).
        """
        self.logger.info(f"Starting {self.mode_label} trading")

        dynamic_spacing = self.config_manager.config.grid_strategy.dynamic_spacing
        if dynamic_spacing.enabled and not self.grid_manager.is_initialized:
            timeframe = self.config_manager.config.trading_settings.timeframe
            period = dynamic_spacing.atr_period
            candles = await self.exchange_service.fetch_recent_ohlcv(self.trading_pair, timeframe, period * 3)
            atr = ATRCalculator.compute(candles, period)
            current_price = await self.exchange_service.get_current_price(self.trading_pair)
            self.grid_manager.regrid(current_price, atr)
            self.logger.info(f"Initial dynamic grid built live (center {current_price}, ATR {atr}).")

        grid_orders_initialized = skip_grid_init

        async def on_ticker_update(current_price):
            async with self._tick_lock:
                token = _IN_TICK.set(True)
                try:
                    await process_ticker_update(current_price)
                finally:
                    _IN_TICK.reset(token)

        async def process_ticker_update(current_price):
            nonlocal grid_orders_initialized
            try:
                if not self._running:
                    self.logger.info("Trading stopped; halting price updates.")
                    return

                account_value = self.balance_tracker.get_total_balance_value(current_price)
                self.live_trading_metrics.append((pd.Timestamp.now(), account_value, current_price))

                grid_orders_initialized = await self._initialize_grid_orders_once(
                    current_price,
                    grid_orders_initialized,
                    skip_initial_purchase=skip_initial_purchase,
                )

                if not grid_orders_initialized:
                    return

                fresh_candle = await self._maybe_refresh_live_atr(pd.Timestamp.now())

                trailing_atr = self._live_atr.get(
                    self.config_manager.config.risk_management.trailing_stop_loss.atr_period,
                    math.nan,
                )
                if await self._handle_trailing_stop(current_price, trailing_atr):
                    return

                if await self._handle_take_profit_stop_loss(current_price):
                    return

                # Volatility regrid is cooldown-counted in bars; only evaluate it once per
                # closed candle, not on every ~3s ticker tick.
                if fresh_candle:
                    dynamic_atr = self._live_atr.get(
                        self.config_manager.config.grid_strategy.dynamic_spacing.atr_period,
                        math.nan,
                    )
                    await self._maybe_regrid_on_volatility(current_price, dynamic_atr)

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
            self._running = False
            kind = StopKind.FEED_LOST if isinstance(e, DataFetchError) else StopKind.CRASH
            await self.event_bus.publish(Events.STOP_BOT, StopRequest(kind, f"Live trading loop failed: {e}"))

        finally:
            self.logger.info("Exiting live/paper trading loop.")

    @staticmethod
    def _timeframe_to_seconds(timeframe: str) -> int:
        # "M" = calendar month, approximated as 30 days (ccxt and the config schema accept "1M").
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2_592_000}
        return int(timeframe[:-1]) * units[timeframe[-1]]

    def _enabled_atr_periods(self) -> set[int]:
        periods: set[int] = set()
        trailing_config = self.config_manager.config.risk_management.trailing_stop_loss
        if trailing_config.enabled:
            periods.add(trailing_config.atr_period)
        dynamic_spacing = self.config_manager.config.grid_strategy.dynamic_spacing
        if dynamic_spacing.enabled:
            periods.add(dynamic_spacing.atr_period)
        return periods

    async def _maybe_refresh_live_atr(self, now: pd.Timestamp) -> bool:
        """
        Fetches recent candles and refreshes self._live_atr for every enabled ATR-based
        feature once a candle boundary has passed. No-op if no such feature is enabled.

        Returns:
            bool: True only when a fresh candle was actually fetched and ingested this call;
            False when no feature is armed, the boundary has not been reached yet, or the
            fetch failed. Callers use this to bar-count volatility regrids per closed candle.
        """
        periods = self._enabled_atr_periods()
        if not periods:
            return False

        timeframe = self.config_manager.config.trading_settings.timeframe
        interval = pd.Timedelta(seconds=self._timeframe_to_seconds(timeframe))
        if self._next_candle_close is None:
            self._next_candle_close = now.ceil(interval)
            return False
        if now < self._next_candle_close:
            return False

        self._next_candle_close = self._next_candle_close + interval
        limit = max(periods) * 3  # enough history for Wilder smoothing to stabilize
        try:
            candles = await self.exchange_service.fetch_recent_ohlcv(self.trading_pair, timeframe, limit)
        except DataFetchError as e:
            self.logger.warning(f"ATR refresh failed, keeping previous ATR: {e}")
            return False

        for period in periods:
            self._live_atr[period] = ATRCalculator.compute(candles, period)
        return True

    def _report_data(self) -> tuple[pd.DataFrame, float, float, float] | None:
        if not self.live_trading_metrics:
            self.logger.warning("No account value data available for live/paper trading mode.")
            return None

        live_data = pd.DataFrame(self.live_trading_metrics, columns=["timestamp", "account_value", "price"])
        live_data = live_data.set_index("timestamp")
        initial_price = live_data.iloc[0]["price"]
        final_price = live_data.iloc[-1]["price"]
        return live_data, initial_price, final_price, live_data.iloc[0]["account_value"]

    def plot_results(self) -> None:
        """Logs that plotting is not available: a live or paper session has no candle history to plot."""
        self.logger.info("Plotting is not available for live/paper trading mode.")
