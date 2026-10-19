"""A real grid, order book, balance tracker and OrderManager on the in-memory execution."""

from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.domain.strategy_type import StrategyType
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order import OrderSide
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.validation.order_validator import OrderValidator

from .fake_execution import FakeExecution

# Buys rest on 80, 90 and 100 and a sell on 120, while 110 stays free.
ARMING_PRICE = 115.0


def stack_config(trading_mode: str = "live", strategy: str = "simple_grid") -> dict:
    return {
        "exchange": {"name": "binance", "trading_fee": 0.001, "trading_mode": trading_mode},
        "pair": {"base_currency": "BTC", "quote_currency": "USDT"},
        "trading_settings": {
            "initial_balance": 2700,
            "timeframe": "1m",
            "period": {"start_date": "2024-07-04T00:00:00Z", "end_date": "2024-07-11T00:00:00Z"},
        },
        "grid_strategy": {
            "type": strategy,
            "spacing": "arithmetic",
            "num_grids": 5,
            "range": {"top": 120, "bottom": 80},
        },
        "risk_management": {
            "take_profit": {"enabled": False, "threshold": 200},
            "stop_loss": {"enabled": False, "threshold": 50},
        },
        "logging": {"log_level": "INFO", "log_to_file": False},
    }


@dataclass
class TradingStack:
    config: ConfigManager
    grid: GridManager
    book: OrderBook
    balances: BalanceTracker
    execution: FakeExecution
    notifications: Mock
    manager: OrderManager


def build_stack(fiat: float = 2700.0, crypto: float = 20.0, trading_mode: str = "live") -> TradingStack:
    config = ConfigManager.from_dict(stack_config(trading_mode=trading_mode))
    grid = GridManager(config, StrategyType.SIMPLE_GRID)
    grid.initialize_grids_and_levels()
    balances = BalanceTracker(
        fee_calculator=FeeCalculator(config),
        trading_mode=TradingMode(trading_mode),
        base_currency="BTC",
        quote_currency="USDT",
    )
    balances._balance = Decimal(str(fiat))
    balances._crypto_balance = Decimal(str(crypto))
    book = OrderBook()
    execution = FakeExecution(market_price=ARMING_PRICE)
    notifications = Mock()
    notifications.async_send_notification = AsyncMock()
    manager = OrderManager(
        grid,
        OrderValidator(),
        balances,
        book,
        execution,
        notifications,
        TradingMode(trading_mode),
        "BTC/USDT",
    )
    return TradingStack(config, grid, book, balances, execution, notifications, manager)


def reserved_matches_open_orders(stack: TradingStack) -> bool:
    """Whether the reserved funds equal what the open orders of the book still need."""
    open_orders = stack.book.get_open_orders()
    fiat = sum(o.price * o.remaining for o in open_orders if o.side == OrderSide.BUY)
    crypto = sum(o.remaining for o in open_orders if o.side == OrderSide.SELL)
    return abs(stack.balances.reserved_fiat - fiat) <= 1e-6 and abs(stack.balances.reserved_crypto - crypto) <= 1e-6
