from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.order_handling.execution_strategy.live_order_execution_strategy import (
    LiveOrderExecutionStrategy,
)
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker


@pytest.fixture
def setup_order_manager():
    grid_manager = Mock()
    order_validator = Mock()
    balance_tracker = Mock()
    balance_tracker.reserve_funds_for_buy = AsyncMock()
    balance_tracker.reserve_funds_for_sell = AsyncMock()
    balance_tracker.update_after_initial_purchase = AsyncMock()
    order_book = Mock()
    event_bus = Mock(spec=EventBus)
    order_execution_strategy = Mock()
    notification_handler = Mock()
    notification_handler.async_send_notification = AsyncMock()
    order_simulator = Mock()
    order_simulator.simulate_fill = AsyncMock()

    manager = OrderManager(
        grid_manager=grid_manager,
        order_validator=order_validator,
        balance_tracker=balance_tracker,
        order_book=order_book,
        event_bus=event_bus,
        order_execution_strategy=order_execution_strategy,
        notification_handler=notification_handler,
        order_simulator=order_simulator,
        trading_mode=TradingMode.LIVE,
        trading_pair="BTC/USDT",
    )
    return (
        manager,
        grid_manager,
        order_validator,
        balance_tracker,
        order_book,
        event_bus,
        order_execution_strategy,
        notification_handler,
    )


@pytest.fixture
def setup_tracker():
    order_book = Mock()
    order_execution_strategy = Mock()
    event_bus = Mock()
    tracker = OrderStatusTracker(
        order_book=order_book,
        order_execution_strategy=order_execution_strategy,
        event_bus=event_bus,
        polling_interval=1.0,
    )
    return tracker, order_book, order_execution_strategy, event_bus


@pytest.fixture
def setup_live_strategy():
    exchange_service = Mock()
    strategy = LiveOrderExecutionStrategy(exchange_service=exchange_service)
    return strategy, exchange_service
