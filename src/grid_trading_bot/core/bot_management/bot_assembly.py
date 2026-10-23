from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import os

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
from grid_trading_bot.core.order_handling.balance_source import BalanceSource
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_factory import (
    OrderExecutionStrategyFactory,
)
from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.order_handling.placement_journal import PlacementJournal
from grid_trading_bot.core.persistence.serializers import compute_config_hash
from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_recovery_service import StateRecoveryService
from grid_trading_bot.core.reconciliation.reconciliation_service import ReconciliationService
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface
from grid_trading_bot.core.services.exchange_service_factory import ExchangeServiceFactory
from grid_trading_bot.core.validation.order_validator import OrderValidator
from grid_trading_bot.strategies.backtest_grid_strategy import BacktestGridStrategy
from grid_trading_bot.strategies.grid_trading_strategy import GridTradingStrategy
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy
from grid_trading_bot.strategies.plotter import Plotter
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer


@dataclass
class BotComponents:
    """The wired components of one bot, built for its configured mode."""

    exchange_service: ExchangeInterface
    grid_manager: GridManager
    balance_tracker: BalanceTracker
    order_book: OrderBook
    journal: PlacementJournal
    reconciliation_service: ReconciliationService | None
    state_persistence_service: StatePersistenceService | None
    state_recovery_service: StateRecoveryService | None
    order_manager: OrderManager
    order_status_tracker: OrderStatusTracker
    strategy: GridTradingStrategy


def assemble_bot(
    config_manager: ConfigManager,
    notification_handler: NotificationHandler,
    event_bus: EventBus,
    exchange_free_base: Callable[[], Awaitable[float]],
) -> BotComponents:
    """Builds and wires the components of a bot with the behaviour its configured trading mode needs."""
    trading_mode = config_manager.config.exchange.trading_mode
    is_live_or_paper = trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}
    base_currency = config_manager.config.pair.base_currency
    quote_currency = config_manager.config.pair.quote_currency
    trading_pair = f"{base_currency}/{quote_currency}"
    strategy_type = config_manager.config.grid_strategy.type

    exchange_service = ExchangeServiceFactory.create_exchange_service(config_manager, trading_mode)
    backtest_clock = BacktestClock()
    order_execution_strategy = OrderExecutionStrategyFactory.create(
        config_manager, exchange_service, backtest_clock=backtest_clock
    )
    grid_manager = GridManager(config_manager, strategy_type)
    order_validator = OrderValidator()
    fee_calculator = FeeCalculator(config_manager)

    fail_fast = trading_mode == TradingMode.BACKTEST
    balance_source = BalanceSource.EXCHANGE if trading_mode == TradingMode.LIVE else BalanceSource.VIRTUAL
    balance_tracker = BalanceTracker(
        fee_calculator=fee_calculator,
        balance_source=balance_source,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    order_book = OrderBook()
    journal = PlacementJournal()

    reconciliation_service = None
    if trading_mode == TradingMode.LIVE:
        reconciliation_service = ReconciliationService(
            order_book=order_book,
            balance_tracker=balance_tracker,
            exchange_service=exchange_service,
            notification_handler=notification_handler,
            event_bus=event_bus,
            trading_pair=trading_pair,
            base_currency=base_currency,
            quote_currency=quote_currency,
            reconciliation_interval=config_manager.config.execution.reconciliation_interval,
            balance_tolerance=config_manager.config.execution.reconciliation_balance_tolerance,
            journal=journal,
        )

    state_persistence_service = None
    state_recovery_service = None
    if is_live_or_paper and config_manager.config.persistence.enabled:
        db_path = config_manager.get_state_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        state_repository = SQLiteStateRepository(db_path)
        state_repository.initialize()

        state_persistence_service = StatePersistenceService(
            repository=state_repository,
            event_bus=event_bus,
            notification_handler=notification_handler,
            order_book=order_book,
            grid_manager=grid_manager,
            balance_tracker=balance_tracker,
            config_manager=config_manager,
            trading_pair=trading_pair,
            strategy_type=strategy_type.value,
            journal=journal,
        )
        state_recovery_service = StateRecoveryService(
            repository=state_repository,
            config_manager=config_manager,
            grid_manager=grid_manager,
            order_book=order_book,
            balance_tracker=balance_tracker,
            exchange_service=exchange_service,
            order_execution_strategy=order_execution_strategy,
            notification_handler=notification_handler,
            trading_pair=trading_pair,
            balance_source=balance_source,
            journal=journal,
        )

    order_manager = OrderManager(
        grid_manager,
        order_validator,
        balance_tracker,
        order_book,
        order_execution_strategy,
        notification_handler,
        fail_fast,
        trading_pair,
        state_store=state_persistence_service,
        journal=journal,
        config_hash=compute_config_hash(config_manager.config),
    )
    if is_live_or_paper:
        order_manager.exchange_balance = exchange_free_base

    order_status_tracker = OrderStatusTracker(
        order_book=order_book,
        order_execution_strategy=order_execution_strategy,
        order_update_handler=order_manager,
        polling_interval=config_manager.config.execution.order_polling_interval,
        event_bus=event_bus,
        notify=notification_handler.async_send_notification,
    )

    trading_performance_analyzer = TradingPerformanceAnalyzer(config_manager, order_book)
    strategy: GridTradingStrategy
    if trading_mode == TradingMode.BACKTEST:
        order_simulator = OrderSimulator(
            order_book=order_book,
            grid_manager=grid_manager,
            order_update_handler=order_manager,
            slippage=config_manager.config.execution.backtest_slippage,
        )
        strategy = BacktestGridStrategy(
            config_manager,
            event_bus,
            exchange_service,
            grid_manager,
            order_manager,
            balance_tracker,
            trading_performance_analyzer,
            trading_pair,
            plotter=Plotter(grid_manager, order_book),
            order_simulator=order_simulator,
            backtest_clock=backtest_clock,
        )
    else:
        strategy = LiveGridStrategy(
            config_manager,
            event_bus,
            exchange_service,
            grid_manager,
            order_manager,
            balance_tracker,
            trading_performance_analyzer,
            trading_pair,
            mode_label="live" if trading_mode == TradingMode.LIVE else "paper",
        )

    if state_persistence_service:
        state_persistence_service.strategy_state_provider = strategy.export_strategy_state

    return BotComponents(
        exchange_service=exchange_service,
        grid_manager=grid_manager,
        balance_tracker=balance_tracker,
        order_book=order_book,
        journal=journal,
        reconciliation_service=reconciliation_service,
        state_persistence_service=state_persistence_service,
        state_recovery_service=state_recovery_service,
        order_manager=order_manager,
        order_status_tracker=order_status_tracker,
        strategy=strategy,
    )
