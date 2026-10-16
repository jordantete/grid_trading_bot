import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import inspect
import logging
import os
import time
import traceback
from typing import TYPE_CHECKING, Any

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.grid_management.grid_manager import GridManager
from grid_trading_bot.core.order_handling.balance_tracker import AccountSnapshot, BalanceTracker
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_factory import (
    OrderExecutionStrategyFactory,
)
from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.persistence.run_files import HaltRecord, RunFiles, write_halt_marker
from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_recovery_service import RecoveryResult, StateRecoveryService
from grid_trading_bot.core.reconciliation.reconciliation_service import ReconciliationService
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    UnsupportedExchangeError,
    UnsupportedTimeframeError,
)
from grid_trading_bot.core.services.exchange_service_factory import ExchangeServiceFactory
from grid_trading_bot.core.services.market_constraints import MarketConstraints
from grid_trading_bot.core.validation.order_validator import OrderValidator
from grid_trading_bot.strategies.grid_trading_strategy import GridTradingStrategy
from grid_trading_bot.strategies.plotter import Plotter
from grid_trading_bot.strategies.trading_performance_analyzer import TradingPerformanceAnalyzer

if TYPE_CHECKING:
    from grid_trading_bot.core.domain.strategy_type import StrategyType

from .notification.notification_content import NotificationType
from .notification.notification_handler import NotificationHandler


class GridTradingBot:
    TICKER_FEED_STALENESS_THRESHOLD = 120.0  # seconds

    def __init__(
        self,
        config_path: str,
        config_manager: ConfigManager,
        notification_handler: NotificationHandler,
        event_bus: EventBus,
        save_performance_results_path: str | None = None,
        no_plot: bool = False,
        run_files: RunFiles | None = None,
    ):
        self.run_files = run_files
        self.stop_request: StopRequest | None = None
        self._starting = False
        self._state_ready = False
        self._shutdown_started = False
        self._stopped = asyncio.Event()
        try:
            self.logger = logging.getLogger(self.__class__.__name__)
            self.config_path = config_path
            self.config_manager = config_manager
            self.notification_handler = notification_handler
            self.event_bus = event_bus
            self.event_bus.subscribe(Events.STOP_BOT, self._handle_stop_bot_event)
            self.save_performance_results_path = save_performance_results_path
            self.no_plot = no_plot
            self.trading_mode: TradingMode = self.config_manager.get_trading_mode()
            base_currency: str = self.config_manager.get_base_currency()
            quote_currency: str = self.config_manager.get_quote_currency()
            trading_pair = f"{base_currency}/{quote_currency}"
            self.trading_pair = trading_pair
            strategy_type: StrategyType = self.config_manager.get_strategy_type()
            self.logger.info(
                f"Starting Grid Trading Bot in {self.trading_mode.value} mode with strategy: {strategy_type.value}",
            )
            self.is_running = False

            self.exchange_service = ExchangeServiceFactory.create_exchange_service(
                self.config_manager,
                self.trading_mode,
            )
            order_execution_strategy = OrderExecutionStrategyFactory.create(self.config_manager, self.exchange_service)
            grid_manager = GridManager(self.config_manager, strategy_type)
            self.grid_manager = grid_manager
            order_validator = OrderValidator()
            fee_calculator = FeeCalculator(self.config_manager)

            self.balance_tracker = BalanceTracker(
                event_bus=self.event_bus,
                fee_calculator=fee_calculator,
                trading_mode=self.trading_mode,
                base_currency=base_currency,
                quote_currency=quote_currency,
            )
            order_book = OrderBook()
            self.order_book = order_book

            self.order_status_tracker = OrderStatusTracker(
                order_book=order_book,
                order_execution_strategy=order_execution_strategy,
                event_bus=self.event_bus,
                polling_interval=self.config_manager.get_order_polling_interval(),
            )

            order_simulator = OrderSimulator(
                order_book=order_book,
                grid_manager=grid_manager,
                event_bus=self.event_bus,
                slippage=self.config_manager.get_backtest_slippage(),
            )

            self.reconciliation_service = None
            if self.trading_mode == TradingMode.LIVE:
                self.reconciliation_service = ReconciliationService(
                    order_book=order_book,
                    balance_tracker=self.balance_tracker,
                    exchange_service=self.exchange_service,
                    notification_handler=self.notification_handler,
                    event_bus=self.event_bus,
                    trading_pair=trading_pair,
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    reconciliation_interval=self.config_manager.get_reconciliation_interval(),
                    balance_tolerance=self.config_manager.get_reconciliation_balance_tolerance(),
                )

            self.state_persistence_service = None
            self.state_recovery_service = None
            if self.trading_mode == TradingMode.LIVE and self.config_manager.is_persistence_enabled():
                db_path = self.config_manager.get_state_db_path()
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                state_repository = SQLiteStateRepository(db_path)
                state_repository.initialize()

                self.state_persistence_service = StatePersistenceService(
                    repository=state_repository,
                    event_bus=self.event_bus,
                    order_book=order_book,
                    grid_manager=grid_manager,
                    balance_tracker=self.balance_tracker,
                    config_manager=self.config_manager,
                    trading_pair=trading_pair,
                    strategy_type=strategy_type.value,
                )
                self.state_recovery_service = StateRecoveryService(
                    repository=state_repository,
                    config_manager=self.config_manager,
                    grid_manager=grid_manager,
                    order_book=order_book,
                    balance_tracker=self.balance_tracker,
                    exchange_service=self.exchange_service,
                    order_execution_strategy=order_execution_strategy,
                    notification_handler=self.notification_handler,
                    trading_pair=trading_pair,
                )

            self.order_manager = OrderManager(
                grid_manager,
                order_validator,
                self.balance_tracker,
                order_book,
                self.event_bus,
                order_execution_strategy,
                self.notification_handler,
                self.trading_mode,
                trading_pair,
            )

            trading_performance_analyzer = TradingPerformanceAnalyzer(self.config_manager, order_book)
            plotter = Plotter(grid_manager, order_book) if self.trading_mode == TradingMode.BACKTEST else None
            self.strategy = GridTradingStrategy(
                self.config_manager,
                self.event_bus,
                self.exchange_service,
                grid_manager,
                self.order_manager,
                self.balance_tracker,
                trading_performance_analyzer,
                self.trading_mode,
                trading_pair,
                plotter,
                order_simulator,
            )

            if self.state_persistence_service:
                self.state_persistence_service.strategy_state_provider = self.strategy.export_strategy_state

        except (UnsupportedExchangeError, DataFetchError, UnsupportedTimeframeError) as e:
            self.logger.error(f"{type(e).__name__}: {e}")
            raise

        except Exception:
            self.logger.error("An unexpected error occurred.")
            self.logger.error(traceback.format_exc())
            raise

    async def run(self) -> dict[str, Any] | None:
        try:
            self.is_running = True
            self._starting = True
            skip_initial_purchase = False
            skip_grid_init = False
            recovery_result = None

            if self.trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}:
                await self.exchange_service.load_exchange_markets(self.trading_pair)
                self.order_manager.set_market_constraints(
                    self.exchange_service.get_market_constraints(self.trading_pair)
                )
            else:
                min_amount = self.config_manager.get_backtest_min_amount()
                min_cost = self.config_manager.get_backtest_min_cost()
                if min_amount is not None or min_cost is not None:
                    self.order_manager.set_market_constraints(MarketConstraints(min_amount, min_cost))

            # Attempt state recovery for live trading
            if self.state_recovery_service:
                self.strategy.initialize_strategy()
                recovery_result = await self.state_recovery_service.attempt_recovery()
                if recovery_result.recovered:
                    skip_initial_purchase = recovery_result.initial_purchase_done
                    skip_grid_init = recovery_result.grid_orders_initialized
                    if self.state_persistence_service:
                        self.state_persistence_service.set_flags(
                            initial_purchase_done=skip_initial_purchase,
                            grid_orders_initialized=skip_grid_init,
                        )
                    if recovery_result.strategy_state is not None:
                        self.strategy.restore_strategy_state(recovery_result.strategy_state)
                    self.logger.info(
                        f"State recovered: skip_initial_purchase={skip_initial_purchase}, "
                        f"skip_grid_init={skip_grid_init}"
                    )
                else:
                    self.logger.warning("Recovery failed; resetting in-memory state for a fresh start.")
                    self.grid_manager.reset()
                    self.order_book.clear()
                    self.strategy.initialize_strategy()
                    await self.balance_tracker.setup_balances(
                        initial_balance=self.config_manager.get_initial_balance(),
                        initial_crypto_balance=0.0,
                        exchange_service=self.exchange_service,
                    )
            else:
                self.strategy.initialize_strategy()
                await self.balance_tracker.setup_balances(
                    initial_balance=self.config_manager.get_initial_balance(),
                    initial_crypto_balance=0.0,
                    exchange_service=self.exchange_service,
                )

            self._starting = False
            self._state_ready = True
            if self.stop_request is None:
                await self._start_trading(recovery_result, skip_initial_purchase, skip_grid_init)
            if self.stop_request is None and self._is_live_or_paper():
                await self._stop(StopRequest(StopKind.CRASH, "Trading loop ended without a stop request"))
            await self._complete_stop()

            if not self.no_plot:
                self.strategy.plot_results()

            return self._generate_and_log_performance()

        except Exception as e:
            self._starting = False
            self.logger.error(f"An unexpected error occurred {e}")
            self.logger.error(traceback.format_exc())
            if self.stop_request is None and self._is_live_or_paper():
                await self._stop(StopRequest(StopKind.CRASH, f"{type(e).__name__}: {e}"))
            await self._complete_stop()
            raise

        finally:
            self.is_running = False

    async def _start_trading(
        self,
        recovery_result: RecoveryResult | None,
        skip_initial_purchase: bool,
        skip_grid_init: bool,
    ) -> None:
        if recovery_result and recovery_result.orders_filled_while_down:
            self.logger.info(
                f"Placing paired orders for {len(recovery_result.orders_filled_while_down)} "
                f"orders filled while bot was down."
            )
            await self.order_manager.place_paired_orders_for_recovered_fills(recovery_result.orders_filled_while_down)

        self.order_status_tracker.start_tracking()
        if self.state_persistence_service:
            self.state_persistence_service.start_periodic_checkpoints(
                self.config_manager.get_checkpoint_interval_seconds()
            )
        if self.reconciliation_service:
            self.reconciliation_service.start()
        await self.strategy.run(
            skip_initial_purchase=skip_initial_purchase,
            skip_grid_init=skip_grid_init,
        )

    def _is_live_or_paper(self) -> bool:
        return self.trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}

    async def request_stop(self, request: StopRequest) -> None:
        """Asks the bot to stop through STOP_BOT, so every component sees the same request."""
        await self.event_bus.publish(Events.STOP_BOT, request)

    async def _handle_stop_bot_event(self, request: StopRequest) -> None:
        self.logger.info(f"Handling STOP_BOT event: {request.kind.value} ({request.reason})")
        await self._stop(request)

    async def _stop(self, request: StopRequest) -> None:
        """
        Records the first stop request and shuts down for it, or defers the shutdown until startup ends when
        it arrives during startup. Later requests are ignored.
        """
        if self.stop_request is not None:
            self.logger.info(
                f"Stop already in progress ({self.stop_request.kind.value}); "
                f"ignoring {request.kind.value}: {request.reason}",
            )
            return
        self.stop_request = request
        if self._starting:
            self.logger.info("Stop requested during startup; it runs once startup ends.")
            return
        await self._shutdown(request)

    async def _complete_stop(self) -> None:
        """Runs the shutdown of a pending stop request, if any, and waits until the shutdown has finished."""
        if self.stop_request is not None:
            await self._shutdown(self.stop_request)
            await self._stopped.wait()

    async def _shutdown(self, request: StopRequest) -> None:
        """
        Runs the shutdown sequence at most once: stops tracking, cancels the open orders only for a halt, writes
        the final checkpoint once the state was loaded and the halt marker, notifies, then closes the exchange last.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.logger.info(f"Stopping Grid Trading Bot ({request.kind.value}): {request.reason}")

        try:
            if self.reconciliation_service:
                await self._attempt("reconciliation", self.reconciliation_service.stop)
            await self._attempt("order tracking", self.order_status_tracker.stop_tracking)
            await self._attempt("strategy", self.strategy.stop)
            if request.kind.halts and self._is_live_or_paper():
                await self._attempt("order cancellation", lambda: self.order_manager.close_positions(liquidate=False))
            if self.state_persistence_service:
                if self._state_ready:
                    await self._attempt("final checkpoint", self.state_persistence_service.checkpoint_now)
                else:
                    self.logger.info("No state was loaded yet; skipping the final checkpoint to keep the saved state.")
                await self._attempt("persistence cleanup", self.state_persistence_service.cleanup)
            if self._is_live_or_paper():
                if request.kind.halts and self.run_files is not None:
                    self._write_halt_marker(request)
                await self._attempt("stop notification", lambda: self._notify_stop(request))
            await self._attempt("subscription cleanup", self._cleanup_subscriptions)
        finally:
            await self._attempt("exchange close", self.exchange_service.close)
            self.is_running = False
            self._stopped.set()
            self.logger.info("Grid Trading Bot has been stopped.")

    async def _attempt(self, step: str, action: Callable[[], Awaitable[Any] | None]) -> None:
        try:
            outcome = action()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as e:
            self.logger.error(f"Stop step '{step}' failed: {e}", exc_info=True)

    def _write_halt_marker(self, request: StopRequest) -> None:
        record = HaltRecord(
            kind=request.kind.value,
            reason=request.reason,
            pair=self.trading_pair,
            halted_at=datetime.now(UTC).isoformat(),
        )
        try:
            write_halt_marker(self.run_files.halt_path, record)
        except OSError as e:
            self.logger.error(f"Could not write the halt marker {self.run_files.halt_path}: {e}", exc_info=True)

    async def _notify_stop(self, request: StopRequest) -> None:
        if request.kind.halts:
            await self.notification_handler.async_send_notification(
                NotificationType.BOT_HALTED,
                kind=request.kind.value,
                reason=request.reason,
                config_path=self.config_path,
            )
        else:
            await self.notification_handler.async_send_notification(
                NotificationType.BOT_SUSPENDED,
                kind=request.kind.value,
                reason=request.reason,
            )

    def _cleanup_subscriptions(self) -> None:
        """Unsubscribes all components from EventBus events."""
        self.event_bus.unsubscribe(Events.STOP_BOT, self._handle_stop_bot_event)
        self.order_manager.cleanup()
        self.balance_tracker.cleanup()
        if self.reconciliation_service:
            self.reconciliation_service.cleanup()
        self.notification_handler.cleanup()

    def _generate_and_log_performance(self) -> dict[str, Any] | None:
        metrics, performance_summary, formatted_orders = self.strategy.generate_performance_report()
        return {
            "config": self.config_path,
            "performance_summary": performance_summary,
            "orders": formatted_orders,
            "metrics": metrics,
        }

    async def get_bot_health_status(self) -> dict:
        health_status = {
            "strategy": await self._check_strategy_health(),
            "exchange_status": await self._get_exchange_status(),
        }

        if self.trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}:
            health_status["ticker_feed"] = self._check_ticker_feed_health()

        health_status["overall"] = all(health_status.values())
        return health_status

    async def _check_strategy_health(self) -> bool:
        if not self.is_running:
            self.logger.warning("Bot has stopped unexpectedly.")
            return False
        return True

    async def _get_exchange_status(self) -> str:
        exchange_status = await self.exchange_service.get_exchange_status()
        return exchange_status.get("status", "unknown")

    def _check_ticker_feed_health(self) -> bool:
        last_update = getattr(self.exchange_service, "last_price_update_ts", None)
        if last_update is None:
            return True  # feed not started yet
        return (time.monotonic() - last_update) < self.TICKER_FEED_STALENESS_THRESHOLD

    def get_balances(self) -> dict[str, float]:
        return {
            "fiat": self.balance_tracker.balance,
            "reserved_fiat": self.balance_tracker.reserved_fiat,
            "crypto": self.balance_tracker.crypto_balance,
            "reserved_crypto": self.balance_tracker.reserved_crypto,
        }

    def get_account_snapshot(self) -> AccountSnapshot | None:
        price = getattr(self.exchange_service, "last_price", None)
        if price is None:
            return None
        return self.balance_tracker.snapshot(price)
