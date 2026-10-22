from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.order_handling.execution_strategy.live_order_execution_strategy import (
    LiveOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker


@pytest.fixture
def setup_order_manager():
    grid_manager = Mock()
    grid_manager.buy_ratio = 1.0
    grid_manager.sell_ratio = 1.0
    order_validator = Mock()
    balance_tracker = Mock()
    balance_tracker.reserve_funds_for_buy = AsyncMock()
    balance_tracker.reserve_funds_for_sell = AsyncMock()
    balance_tracker.release_reserved_fiat = AsyncMock()
    balance_tracker.release_reserved_crypto = AsyncMock()
    balance_tracker.update_after_initial_purchase = AsyncMock()
    balance_tracker.update_after_liquidation = AsyncMock()
    balance_tracker.record_execution = AsyncMock()
    balance_tracker.fee_calculator.order_base_fee.return_value = 0.0
    order_book = Mock()
    order_book.has_open_orders_at.return_value = False
    order_execution_strategy = Mock()
    notification_handler = Mock()
    notification_handler.async_send_notification = AsyncMock()

    manager = OrderManager(
        grid_manager=grid_manager,
        order_validator=order_validator,
        balance_tracker=balance_tracker,
        order_book=order_book,
        order_execution_strategy=order_execution_strategy,
        notification_handler=notification_handler,
        trading_mode=TradingMode.LIVE,
        trading_pair="BTC/USDT",
    )
    return (
        manager,
        grid_manager,
        order_validator,
        balance_tracker,
        order_book,
        order_execution_strategy,
        notification_handler,
    )


@pytest.fixture
def setup_tracker():
    order_book = Mock()
    order_book.get_open_orders.return_value = []
    order_execution_strategy = Mock()
    order_update_handler = Mock()
    order_update_handler.apply_order_update = AsyncMock()
    order_update_handler.retry_pending_placements = AsyncMock()
    tracker = OrderStatusTracker(
        order_book=order_book,
        order_execution_strategy=order_execution_strategy,
        order_update_handler=order_update_handler,
        polling_interval=1.0,
    )
    return tracker, order_book, order_execution_strategy, order_update_handler


@pytest.fixture
def setup_live_strategy():
    exchange_service = Mock()
    strategy = LiveOrderExecutionStrategy(exchange_service=exchange_service)
    return strategy, exchange_service
