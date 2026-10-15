import logging
from typing import Any

import numpy as np
import pandas as pd
from tabulate import tabulate

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.grid_management.grid_level import GridLevel
from grid_trading_bot.core.order_handling.order import Order
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.strategies.performance_metrics import PerformanceMetrics
from grid_trading_bot.utils.constants import TIMEFRAME_MAPPINGS

ANNUAL_RISK_FREE_RATE = 0.03  # annual risk free rate 3%
MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000  # crypto trades 24/7, so a full calendar year
MIN_STD_DEV = 1e-9  # below this, volatility is float noise on a flat account value


class TradingPerformanceAnalyzer:
    def __init__(
        self,
        config_manager: ConfigManager,
        order_book: OrderBook,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_manager: ConfigManager = config_manager
        self.order_book: OrderBook = order_book
        self.base_currency, self.quote_currency, self.trading_fee = self._extract_config()

    def _extract_config(self) -> tuple[str, str, float]:
        """
        Extract trading-related configuration values.

        Returns:
            Tuple[str, str, float]: Base currency, quote currency, and trading fee.
        """
        base_currency = self.config_manager.get_base_currency()
        quote_currency = self.config_manager.get_quote_currency()
        trading_fee = self.config_manager.get_trading_fee()
        return base_currency, quote_currency, trading_fee

    def _calculate_roi(
        self,
        initial_balance: float,
        final_balance: float,
    ) -> float:
        """
        Calculate the return on investment (ROI) percentage.

        Args:
            Initial_balance (float): The initial account balance.
            final_balance (float): The final account balance.

        Returns:
            float: The calculated ROI percentage.
        """
        roi = (final_balance - initial_balance) / initial_balance * 100
        return round(roi, 2)

    def _calculate_grid_trading_gains(self, realized_pnl: float) -> float | None:
        """Realized PnL, or None when nothing was ever sold (liquidation sells never enter the order book)."""
        has_filled_sell = any(order.is_filled() for order in self.order_book.get_all_sell_orders())
        if not has_filled_sell and realized_pnl == 0:
            return None
        return realized_pnl

    def _calculate_drawdown(self, data: pd.DataFrame) -> float:
        peak = data["account_value"].expanding(min_periods=1).max()
        drawdown = (peak - data["account_value"]) / peak * 100
        max_drawdown = drawdown.max()
        return max_drawdown

    def _calculate_max_drawdown_duration(self, data: pd.DataFrame) -> pd.Timedelta:
        """Longest time from an account value peak to its recovery, or to the last row when it never recovers."""
        values = data["account_value"].dropna()
        at_peak = values >= values.cummax()
        underwater = ~at_peak
        in_drawdown = underwater | underwater.shift(1, fill_value=False)
        if not in_drawdown.any():
            return pd.Timedelta(0)
        timestamps = values.index.to_series()
        last_peak_time = timestamps.where(at_peak).ffill().shift(1)
        return (timestamps - last_peak_time)[in_drawdown].max()

    def _calculate_runup(self, data: pd.DataFrame) -> float:
        trough = data["account_value"].expanding(min_periods=1).min()
        runup = (data["account_value"] - trough) / trough * 100
        max_runup = runup.max()
        return max_runup

    def _calculate_time_in_profit_loss(
        self,
        initial_balance: float,
        data: pd.DataFrame,
    ) -> tuple[float, float]:
        time_in_profit = (data["account_value"] > initial_balance).mean() * 100
        time_in_loss = (data["account_value"] <= initial_balance).mean() * 100
        return time_in_profit, time_in_loss

    def _periods_per_year(self) -> float:
        """Number of candles in a year for the configured timeframe (crypto trades 24/7)."""
        return MS_PER_YEAR / TIMEFRAME_MAPPINGS[self.config_manager.get_timeframe()]

    def _calculate_sharpe_ratio(self, data: pd.DataFrame) -> float:
        """
        Calculate the annualized Sharpe ratio based on the account value,
        using per-candle returns at the configured timeframe.

        Args:
            data (pd.DataFrame): Historical account value data.

        Returns:
            float: The Sharpe ratio.
        """
        periods_per_year = self._periods_per_year()
        returns = data["account_value"].pct_change()
        excess_returns = returns - ANNUAL_RISK_FREE_RATE / periods_per_year
        std_dev = excess_returns.std()
        if not std_dev > MIN_STD_DEV:  # also catches NaN
            return 0.0
        sharpe_ratio = excess_returns.mean() / std_dev * np.sqrt(periods_per_year)
        return round(sharpe_ratio, 2)

    def _calculate_sortino_ratio(self, data: pd.DataFrame) -> float:
        """
        Calculate the annualized Sortino ratio based on the account value,
        using per-candle returns at the configured timeframe.

        Args:
            data (pd.DataFrame): Historical account value data.

        Returns:
            float: The Sortino ratio.
        """
        periods_per_year = self._periods_per_year()
        returns = data["account_value"].pct_change()
        excess_returns = returns - ANNUAL_RISK_FREE_RATE / periods_per_year
        downside_returns = excess_returns[excess_returns < 0]
        downside_std = downside_returns.std()

        if len(downside_returns) == 0 or not downside_std > MIN_STD_DEV:
            return round(excess_returns.mean() * np.sqrt(periods_per_year), 2)  # Positive ratio if no downside

        sortino_ratio = excess_returns.mean() / downside_std * np.sqrt(periods_per_year)
        return round(sortino_ratio, 2)

    def get_formatted_orders(self) -> list[list[str | float]]:
        """
        Retrieve a formatted list of filled buy and sell orders.

        Returns:
            List[List[Union[str, float]]]: Formatted orders with details like side, type,
            status, price, quantity, timestamp, etc.
        """
        orders = []
        buy_orders_with_grid = self.order_book.get_buy_orders_with_grid()
        sell_orders_with_grid = self.order_book.get_sell_orders_with_grid()

        for buy_order, grid_level in buy_orders_with_grid:
            if buy_order.is_filled():
                orders.append(self._format_order(buy_order, grid_level))

        for sell_order, grid_level in sell_orders_with_grid:
            if sell_order.is_filled():
                orders.append(self._format_order(sell_order, grid_level))

        orders.sort(key=lambda x: (x[5] is None, x[5]))  # x[5] is the timestamp, sort None to the end
        return orders

    def _format_order(self, order: Order, grid_level: GridLevel | None) -> list[str | float]:
        grid_level_price = grid_level.price if grid_level else "N/A"
        if grid_level and order.average is not None:
            # Assuming order.price is the execution price and grid level price the expected price
            slippage = ((order.average - grid_level_price) / grid_level_price) * 100
            slippage_str = f"{slippage:.2f}%"
        else:
            slippage = "N/A"
            slippage_str = "N/A"
        return [
            order.side.name,
            order.order_type.name,
            order.status.name,
            order.price,
            order.filled,
            order.format_last_trade_timestamp(),
            grid_level_price,
            slippage_str,
        ]

    def _calculate_trade_counts(self) -> tuple[int, int]:
        """
        Count the number of filled buy and sell orders.

        Returns:
            Tuple[int, int]: Number of buy trades and number of sell trades.
        """
        num_buy_trades = len([order for order in self.order_book.get_all_buy_orders() if order.is_filled()])
        num_sell_trades = len([order for order in self.order_book.get_all_sell_orders() if order.is_filled()])
        return num_buy_trades, num_sell_trades

    def _calculate_buy_and_hold_return(
        self,
        data: pd.DataFrame,
        initial_price: float,
        final_price: float,
    ) -> float:
        """
        Calculate the buy-and-hold return percentage.

        Args:
            data (pd.DataFrame): Historical price data.
            initial_price (float): The initial cryptocurrency price.
            final_price (float): The final cryptocurrency price.

        Returns:
            float: The buy-and-hold return percentage.
        """
        return ((final_price - initial_price) / initial_price) * 100

    def _calculate_avg_crypto_exposure(self, data: pd.DataFrame) -> float | None:
        """Mean share of the account held in crypto over the candles; None without a crypto_value column (live)."""
        if "crypto_value" not in data.columns:
            return None
        return float((data["crypto_value"] / data["account_value"]).mean())

    def generate_performance_summary(
        self,
        data: pd.DataFrame,
        initial_price: float,
        final_fiat_balance: float,
        final_crypto_balance: float,
        final_crypto_price: float,
        total_fees: float,
        realized_pnl: float,
        unrealized_pnl: float,
        *,
        initial_balance: float,
        armed: bool,
        stop_reason: str | None,
    ) -> tuple[PerformanceMetrics, dict[str, Any], list[list[str | float]]]:
        """
        Generate a detailed performance summary for the trading session.

        Args:
            data (pd.DataFrame): Account value and price data.
            final_fiat_balance (float): Final fiat currency balance.
            final_crypto_balance (float): Final cryptocurrency balance.
            final_crypto_price (float): Final cryptocurrency price.
            total_fees (float): Total trading fees incurred.
            realized_pnl (float): Realized PnL at the weighted average cost.
            unrealized_pnl (float): Unrealized PnL of the crypto still held, at the final price.
            initial_balance (float): Account value before any trade, the baseline of the ROI.
            armed (bool): Whether the grid was armed during the session.
            stop_reason (str | None): Why trading stopped, or None if it ran to the end.

        Returns:
            tuple: The typed metrics, a dictionary of display metrics and a list of formatted orders.
        """
        pair = f"{self.base_currency}/{self.quote_currency}"
        start_date = data.index[0]
        end_date = data.index[-1]
        duration = end_date - start_date
        final_crypto_value = final_crypto_balance * final_crypto_price
        final_balance = final_fiat_balance + final_crypto_value
        max_drawdown_duration = self._calculate_max_drawdown_duration(data)
        max_runup = self._calculate_runup(data)
        time_in_profit, time_in_loss = self._calculate_time_in_profit_loss(initial_balance, data)
        num_buy_trades, num_sell_trades = self._calculate_trade_counts()
        metrics = PerformanceMetrics(
            roi_pct=float(self._calculate_roi(initial_balance, final_balance)),
            max_drawdown_pct=float(self._calculate_drawdown(data)),
            max_drawdown_duration_hours=max_drawdown_duration / pd.Timedelta(hours=1),
            sharpe_ratio=float(self._calculate_sharpe_ratio(data)),
            sortino_ratio=float(self._calculate_sortino_ratio(data)),
            buy_and_hold_return_pct=float(self._calculate_buy_and_hold_return(data, initial_price, final_crypto_price)),
            grid_trading_gains=self._calculate_grid_trading_gains(realized_pnl),
            unrealized_pnl=float(unrealized_pnl),
            total_fees=float(total_fees),
            final_balance=float(final_balance),
            num_buy_trades=num_buy_trades,
            num_sell_trades=num_sell_trades,
            avg_crypto_exposure=self._calculate_avg_crypto_exposure(data),
            equity_coverage=float(data["account_value"].notna().mean()),
            armed=armed,
            stop_reason=stop_reason,
        )

        performance_summary = {
            "Pair": pair,
            "Start Date": start_date,
            "End Date": end_date,
            "Duration": duration,
            "ROI": f"{metrics.roi_pct:.2f}%",
            "Max Drawdown": f"{metrics.max_drawdown_pct:.2f}%",
            "Max Drawdown Duration": max_drawdown_duration,
            "Max Runup": f"{max_runup:.2f}%",
            "Time in Profit %": f"{time_in_profit:.2f}%",
            "Time in Loss %": f"{time_in_loss:.2f}%",
            "Buy and Hold Return %": f"{metrics.buy_and_hold_return_pct:.2f}%",
            "Grid Trading Gains": "N/A" if metrics.grid_trading_gains is None else f"{metrics.grid_trading_gains:.2f}",
            "Unrealized PnL": f"{metrics.unrealized_pnl:.2f}",
            "Total Fees": f"{metrics.total_fees:.2f}",
            "Final Balance (Fiat)": f"{metrics.final_balance:.2f}",
            "Final Crypto Balance": f"{final_crypto_balance:.4f} {self.base_currency}",
            "Final Crypto Value (Fiat)": f"{final_crypto_value:.2f} {self.quote_currency}",
            "Remaining Fiat Balance": f"{final_fiat_balance:.2f} {self.quote_currency}",
            "Number of Buy Trades": metrics.num_buy_trades,
            "Number of Sell Trades": metrics.num_sell_trades,
            "Sharpe Ratio": f"{metrics.sharpe_ratio:.2f}",
            "Sortino Ratio": f"{metrics.sortino_ratio:.2f}",
        }

        formatted_orders = self.get_formatted_orders()

        orders_table = tabulate(
            formatted_orders,
            headers=["Order Side", "Type", "Status", "Price", "Quantity", "Timestamp", "Grid Level", "Slippage"],
            tablefmt="pipe",
        )
        self.logger.info("\nFormatted Orders:\n" + orders_table)

        summary_table = tabulate(performance_summary.items(), headers=["Metric", "Value"], tablefmt="grid")
        self.logger.info("\nPerformance Summary:\n" + summary_table)

        return metrics, performance_summary, formatted_orders
