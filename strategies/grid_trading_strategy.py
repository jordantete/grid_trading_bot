import logging, itertools
from typing import Optional, Tuple, Union
import pandas as pd
import numpy as np
from .base import TradingStrategy
from config.trading_mode import TradingMode
from core.order_handling.order import OrderSide
from config.config_manager import ConfigManager
from core.services.exchange_interface import ExchangeInterface
from core.grid_management.grid_manager import GridManager
from core.order_handling.order_manager import OrderManager
from core.order_handling.balance_tracker import BalanceTracker
from strategies.trading_performance_analyzer import TradingPerformanceAnalyzer
from strategies.plotter import Plotter

TICKER_REFRESH_INTERVAL = 3  # in seconds

class GridTradingStrategy(TradingStrategy):
    def __init__(
        self,
        config_manager: ConfigManager,
        exchange_service: ExchangeInterface,
        grid_manager: GridManager,
        order_manager: OrderManager,
        balance_tracker: BalanceTracker,
        trading_performance_analyzer: TradingPerformanceAnalyzer,
        plotter: Optional[Plotter] = None
    ):
        super().__init__(config_manager, balance_tracker)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchange_service = exchange_service
        self.grid_manager = grid_manager
        self.order_manager = order_manager
        self.trading_performance_analyzer = trading_performance_analyzer
        self.plotter = plotter
        self.trading_mode = self.config_manager.get_trading_mode()
        self.data = self._initialize_data() if self.trading_mode == TradingMode.BACKTEST else None
        self._running = True
    
    def _initialize_data(self) -> Optional[pd.DataFrame]:
        try:
            pair, timeframe, start_date, end_date = self._extract_config()
            return self.exchange_service.fetch_ohlcv(pair, timeframe, start_date, end_date)
        except Exception as e:
            self.logger.error(f"Failed to initialize data for backtest trading mode: {e}")
            return None
    
    def _extract_config(self) -> Tuple[str, str, str, str]:
        pair = f"{self.config_manager.get_base_currency()}/{self.config_manager.get_quote_currency()}"
        timeframe = self.config_manager.get_timeframe()
        start_date = self.config_manager.get_start_date()
        end_date = self.config_manager.get_end_date()
        return pair, timeframe, start_date, end_date

    def initialize_strategy(self):
        self.grid_manager.initialize_grids_and_levels()
    
    async def stop(self):
        self._running = False
        await self.exchange_service.close_connection()
        self.logger.info("Trading execution stopped.")

    async def restart(self):
        if not self._running:
            self._running = True
            self.logger.info("Restarting trading session.")
            await self.run()

    async def run(self):
        if self.trading_mode == TradingMode.BACKTEST:
            await self._run_backtest()
            self.logger.info("Ending backtest simulation")
        else:
            await self._run_live_or_paper_trading()
    
    async def _run_live_or_paper_trading(self):
        self.logger.info(f"Starting {'live' if self.trading_mode == TradingMode.LIVE else 'paper'} trading")
        pair = f"{self.config_manager.get_base_currency()}/{self.config_manager.get_quote_currency()}"
        last_price: Optional[float] = None

        async def on_ticker_update(current_price, timestamp):
            nonlocal last_price
            
            if not self._running:
                self.logger.info("Trading stopped; halting price updates.")
                return

            if last_price is not None:
                await self._execute_orders(current_price, last_price, timestamp)

            if await self._check_take_profit_stop_loss(current_price, timestamp):
                self.logger.info("Take-profit or stop-loss triggered, ending trading session.")
                await self.stop()
                return

            last_price = current_price
        await self.exchange_service.listen_to_ticker_updates(pair, on_ticker_update, TICKER_REFRESH_INTERVAL)

    async def _run_backtest(self) -> None:
        if self.data is None:
            self.logger.error("No data available for backtesting.")
            return

        self.logger.info("Starting backtest simulation")
        self.data['account_value'] = np.nan
        self.close_prices = self.data['close'].values
        timestamps = self.data.index
        initial_price = self.close_prices[0]
        self.data.loc[timestamps[0], 'account_value'] = await self.balance_tracker.get_total_balance_value(initial_price)

        for (current_price, previous_price), current_timestamp in zip(itertools.pairwise(self.close_prices), timestamps[1:]):
            if await self._check_take_profit_stop_loss(current_price, current_timestamp):
                break
            await self._execute_orders(current_price, previous_price, current_timestamp)
            self.data.loc[current_timestamp, 'account_value'] = await self.balance_tracker.get_total_balance_value(current_price)
    
    def generate_performance_report(self) -> Tuple[dict, list]:
        final_price = self.close_prices[-1]
        return self.trading_performance_analyzer.generate_performance_summary(
            self.data, 
            self.balance_tracker.balance, 
            self.balance_tracker.crypto_balance, 
            final_price,
            self.balance_tracker.total_fees
        )

    def plot_results(self) -> None:
        if self.trading_mode == TradingMode.BACKTEST:
            self.plotter.plot_results(self.data)
        else:
            self.logger.info("Plotting is not available for live/paper trading mode.")
    
    async def _execute_orders(
        self, 
        current_price: float, 
        previous_price: float, 
        current_timestamp: Union[int, str]
    ) -> None:
        await self.order_manager.execute_order(OrderSide.BUY, current_price, previous_price, current_timestamp)
        await self.order_manager.execute_order(OrderSide.SELL, current_price, previous_price, current_timestamp)

    async def _check_take_profit_stop_loss(
        self, 
        current_price: float, 
        current_timestamp: Union[int, str]
    ) -> bool:
        if self.balance_tracker.crypto_balance == 0:
            return False

        if self.config_manager.is_take_profit_enabled() and current_price >= self.config_manager.get_take_profit_threshold():
            await self.order_manager.execute_take_profit_or_stop_loss_order(
                current_price=current_price, 
                timestamp=current_timestamp,
                take_profit_order=True
            )
            return True

        if self.config_manager.is_stop_loss_enabled() and current_price <= self.config_manager.get_stop_loss_threshold():
            await self.order_manager.execute_take_profit_or_stop_loss_order(
                current_price=current_price,
                timestamp=current_timestamp, 
                stop_loss_order=True
            )
            return True

        return False