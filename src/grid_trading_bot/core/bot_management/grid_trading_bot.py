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
from grid_trading_bot.core.order_handling.backtest_clock import BacktestClock
from grid_trading_bot.core.order_handling.balance_tracker import AccountSnapshot, BalanceTracker
from grid_trading_bot.core.order_handling.execution_strategy.order_execution_strategy_factory import (
    OrderExecutionStrategyFactory,
)
from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.order_simulator import OrderSimulator
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.order_handling.placement_journal import PlacementJournal
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.persistence.run_files import HaltRecord, RunFiles, write_halt_marker
from grid_trading_bot.core.persistence.serializers import compute_config_hash
from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_recovery_service import (
    RecoveryOutcome,
    RecoveryResult,
    StateRecoveryService,
)
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

_SUSPEND_KINDS = frozenset({StopKind.SIGNAL, StopKind.FEED_LOST, StopKind.CRASH})


class GridTradingBot:
    TICKER_FEED_STALENESS_THRESHOLD = 120.0  # seconds
    TICK_DRAIN_TIMEOUT = 20.0  # seconds
    CHECKPOINT_LOCK_TIMEOUT = 5.0  # seconds

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
        self._request_settled = False
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
            backtest_clock = BacktestClock()
            order_execution_strategy = OrderExecutionStrategyFactory.create(
                self.config_manager, self.exchange_service, backtest_clock=backtest_clock
            )
            grid_manager = GridManager(self.config_manager, strategy_type)
            self.grid_manager = grid_manager
            order_validator = OrderValidator()
            fee_calculator = FeeCalculator(self.config_manager)

            self.balance_tracker = BalanceTracker(
                fee_calculator=fee_calculator,
                trading_mode=self.trading_mode,
                base_currency=base_currency,
                quote_currency=quote_currency,
            )
            order_book = OrderBook()
            self.order_book = order_book
            journal = PlacementJournal()

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
                    journal=journal,
                )

            self.state_persistence_service = None
            self.state_recovery_service = None
            if self._is_live_or_paper() and self.config_manager.is_persistence_enabled():
                db_path = self.config_manager.get_state_db_path()
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                state_repository = SQLiteStateRepository(db_path)
                state_repository.initialize()

                self.state_persistence_service = StatePersistenceService(
                    repository=state_repository,
                    event_bus=self.event_bus,
                    notification_handler=self.notification_handler,
                    order_book=order_book,
                    grid_manager=grid_manager,
                    balance_tracker=self.balance_tracker,
                    config_manager=self.config_manager,
                    trading_pair=trading_pair,
                    strategy_type=strategy_type.value,
                    journal=journal,
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
                    journal=journal,
                )

            self.order_manager = OrderManager(
                grid_manager,
                order_validator,
                self.balance_tracker,
                order_book,
                order_execution_strategy,
                self.notification_handler,
                self.trading_mode,
                trading_pair,
                state_store=self.state_persistence_service,
                journal=journal,
                config_hash=compute_config_hash(self.config_manager),
            )
            if self._is_live_or_paper():
                self.order_manager.exchange_balance = self._exchange_free_base

            self.order_status_tracker = OrderStatusTracker(
                order_book=order_book,
                order_execution_strategy=order_execution_strategy,
                order_update_handler=self.order_manager,
                polling_interval=self.config_manager.get_order_polling_interval(),
                event_bus=self.event_bus,
                notify=self.notification_handler.async_send_notification,
            )

            order_simulator = OrderSimulator(
                order_book=order_book,
                grid_manager=grid_manager,
                order_update_handler=self.order_manager,
                slippage=self.config_manager.get_backtest_slippage(),
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
                backtest_clock=backtest_clock,
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
            if self.stop_request is not None:
                await self._complete_stop()
                return self._report_results()

            self._starting = True
            skip_initial_purchase = False
            skip_grid_init = False
            recovery_result = None

            if self.trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}:
                await self.exchange_service.load_exchange_markets(self.trading_pair)
                constraints = self.exchange_service.get_market_constraints(self.trading_pair)
                self.order_manager.set_market_constraints(constraints)
                if self.reconciliation_service:
                    self.reconciliation_service.set_market_constraints(constraints)
            else:
                min_amount = self.config_manager.get_backtest_min_amount()
                min_cost = self.config_manager.get_backtest_min_cost()
                if min_amount is not None or min_cost is not None:
                    self.order_manager.set_market_constraints(MarketConstraints(min_amount, min_cost))

            # Attempt state recovery for live trading
            refused = False
            if self.state_recovery_service:
                self.strategy.initialize_strategy()
                recovery_result = await self.state_recovery_service.attempt_recovery()
                if recovery_result.recovered:
                    skip_initial_purchase = recovery_result.initial_purchase_done
                    skip_grid_init = recovery_result.grid_orders_initialized
                    self.order_manager.restore_arming(ArmingFlags(skip_initial_purchase, skip_grid_init))
                    if recovery_result.strategy_state is not None:
                        self.strategy.restore_strategy_state(recovery_result.strategy_state)
                    self.logger.info(
                        f"State recovered: skip_initial_purchase={skip_initial_purchase}, "
                        f"skip_grid_init={skip_grid_init}"
                    )
                    if recovery_result.liquidation_in_progress:
                        await self._halt_for_interrupted_liquidation()
                elif recovery_result.outcome is RecoveryOutcome.NO_STATE:
                    refused = not await self._start_fresh()
                else:
                    refused = True
                    await self._refuse_start(recovery_result)
            else:
                self.strategy.initialize_strategy()
                refused = not await self._start_fresh()

            if not refused:
                self._state_ready = True
                # Placed even when a stop arrived during recovery: recovery already took these orders out of the open
                # orders, so a final checkpoint without their successors would lose them. After an interrupted
                # liquidation the bot halts and cancels the grid, so it places none.
                if recovery_result is not None and not recovery_result.liquidation_in_progress:
                    if recovery_result.orders_filled_while_down:
                        self.logger.info(
                            f"Placing paired orders for {len(recovery_result.orders_filled_while_down)} "
                            f"orders filled while bot was down."
                        )
                        await self.order_manager.place_paired_orders_for_recovered_fills(
                            recovery_result.orders_filled_while_down
                        )
                    if recovery_result.ended_while_down:
                        self.logger.info(
                            f"Re-placing {len(recovery_result.ended_while_down)} orders that ended while bot was down."
                        )
                        await self.order_manager.place_replacements_for_recovered_ends(recovery_result.ended_while_down)
            self._starting = False

            # _start_trading does not yield before strategy.run, so this one check covers everything it starts.
            if self.stop_request is None:
                await self._start_trading(skip_initial_purchase, skip_grid_init)
            if self.stop_request is None and self._is_live_or_paper():
                await self._stop(StopRequest(StopKind.CRASH, "Trading loop ended without a stop request"))
            await self._complete_stop()
            return self._report_results()

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

    async def _start_fresh(self) -> bool:
        """
        Sets up the balances of a fresh grid, after checking in live and paper trading that no order is still open
        on the pair, since no saved state covers one. Returns False when the start is refused or deferred.
        """
        if self._is_live_or_paper():
            try:
                open_orders = await self.exchange_service.fetch_open_orders(self.trading_pair)
            except DataFetchError as e:
                await self._stop(
                    StopRequest(
                        StopKind.EXCHANGE_UNAVAILABLE,
                        f"the open orders on {self.trading_pair} could not be checked ({e})",
                    )
                )
                return False
            if open_orders:
                listed = ", ".join(str(order.get("id")) for order in open_orders[:10])
                if len(open_orders) > 10:
                    listed += f", and {len(open_orders) - 10} more"
                await self._stop(
                    StopRequest(
                        StopKind.START_REFUSED,
                        f"{len(open_orders)} order(s) are still open on {self.trading_pair} ({listed}) and no saved "
                        "state covers them. Cancel them on the exchange, or restore the state database that placed "
                        "them.",
                    )
                )
                return False
        await self.balance_tracker.setup_balances(
            initial_balance=self.config_manager.get_initial_balance(),
            initial_crypto_balance=0.0,
            exchange_service=self.exchange_service,
        )
        return True

    async def _halt_for_interrupted_liquidation(self) -> None:
        """Halts instead of resuming the grid: the liquidation the bot was running when it stopped may be unfinished."""
        await self._attempt(
            "interrupted liquidation alert",
            lambda: self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details=(
                    "A liquidation was in progress when the bot stopped. What it sold is booked, but the position "
                    "may still be open on the exchange: check it and sell what is left by hand. The bot halts "
                    "instead of resuming the grid."
                ),
            ),
        )
        await self._stop(StopRequest(StopKind.RISK, "a liquidation was in progress when the bot stopped"))

    async def _refuse_start(self, result: RecoveryResult) -> None:
        """Stops before trading when the saved state cannot be resumed; the database is left as it is."""
        db_path = self.config_manager.get_state_db_path()
        if result.outcome is RecoveryOutcome.DEFERRED:
            await self._stop(
                StopRequest(StopKind.EXCHANGE_UNAVAILABLE, f"{result.reason}; the saved state {db_path} is kept")
            )
            return
        await self._stop(
            StopRequest(
                StopKind.START_REFUSED,
                f"{result.reason}. The saved state {db_path} is untouched: relaunch to retry once the cause is "
                "fixed, or move it aside to start a fresh grid.",
            )
        )

    async def _start_trading(self, skip_initial_purchase: bool, skip_grid_init: bool) -> None:
        """Starts order tracking, periodic checkpoints, reconciliation and the strategy."""
        self.order_status_tracker.start_tracking()
        if self.state_persistence_service:
            self.state_persistence_service.start_periodic_checkpoints(
                self.config_manager.get_checkpoint_interval_seconds(), self.order_manager.checkpoint
            )
        if self.reconciliation_service:
            self.reconciliation_service.start()
        await self.strategy.run(
            skip_initial_purchase=skip_initial_purchase,
            skip_grid_init=skip_grid_init,
        )

    def _report_results(self) -> dict[str, Any] | None:
        if not self.no_plot:
            self.strategy.plot_results()
        return self._generate_and_log_performance()

    def _is_live_or_paper(self) -> bool:
        return self.trading_mode in {TradingMode.LIVE, TradingMode.PAPER_TRADING}

    async def _exchange_free_base(self) -> float:
        """
        The base currency free on the exchange, which a liquidation the exchange refused falls back on. Raises
        DataFetchError when the exchange does not report it.
        """
        base = self.config_manager.get_base_currency()
        free = (await self.exchange_service.get_balance() or {}).get("free") or {}
        if free.get(base) is None:
            raise DataFetchError(f"The exchange reported no free {base} balance.")
        return float(free[base])

    async def request_stop(self, request: StopRequest) -> None:
        """Asks the bot to stop through STOP_BOT, so every component sees the same request."""
        await self.event_bus.publish(Events.STOP_BOT, request)

    async def _handle_stop_bot_event(self, request: StopRequest) -> None:
        self.logger.info(f"Handling STOP_BOT event: {request.kind.value} ({request.reason})")
        await self._stop(request)

    async def _stop(self, request: StopRequest) -> None:
        """
        Records the first stop request and shuts down for it, or defers the shutdown until startup ends when it
        arrives during startup. A halt that arrives while a suspend is recorded replaces it until the shutdown has
        read the request, so a stop-loss fired during a SIGTERM drain still halts; any other later request is ignored.
        """
        if self.stop_request is not None:
            if request.kind.halts and self.stop_request.kind in _SUSPEND_KINDS and not self._request_settled:
                self.logger.warning(
                    f"Halt requested ({request.kind.value}: {request.reason}) while stopping for "
                    f"{self.stop_request.kind.value}; the bot halts instead."
                )
                self.stop_request = request
                return
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
        Runs the shutdown sequence at most once: stops tracking and the strategy, interrupts a liquidation's retries,
        waits for the ticker update in progress, then settles the request (a halt fired by that update replaces a
        suspend), writes the halt marker then cancels the open orders only for a halt, writes the final checkpoint
        once the state was loaded, notifies, then closes the exchange last.
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
            await self._attempt("liquidation interrupt", self.order_manager.interrupt_liquidation)
            await self._attempt("ticker update", lambda: self.strategy.wait_for_tick_to_finish(self.TICK_DRAIN_TIMEOUT))
            self._request_settled = True
            request = self.stop_request or request
            if request.kind.halts and self._is_live_or_paper():
                # Marked first, so a SIGKILL during a slow cancellation cannot leave a halted bot without its marker.
                if self.run_files is not None:
                    await self._record_halt(request, self.run_files.halt_path)
                await self._attempt("order cancellation", lambda: self.order_manager.close_positions(liquidate=False))
            if self.state_persistence_service:
                if self._state_ready:
                    await self._attempt(
                        "final checkpoint",
                        lambda: self.order_manager.checkpoint(lock_timeout=self.CHECKPOINT_LOCK_TIMEOUT),
                    )
                else:
                    self.logger.info("No state was loaded yet; skipping the final checkpoint to keep the saved state.")
                await self._attempt("persistence cleanup", self.state_persistence_service.cleanup)
            if self._is_live_or_paper():
                await self._attempt("stop notification", lambda: self._notify_stop(request))
            await self._attempt("subscription cleanup", self._cleanup_subscriptions)
        finally:
            await self._attempt("exchange close", self.exchange_service.close)
            self.is_running = False
            self._stopped.set()
            self.logger.info("Grid Trading Bot has been stopped.")

    async def _attempt(self, step: str, action: Callable[[], Awaitable[Any] | None]) -> bool:
        """Runs one stop step, logging its failure instead of raising it. Returns whether it succeeded."""
        try:
            outcome = action()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as e:
            self.logger.error(f"Stop step '{step}' failed: {e}", exc_info=True)
            return False
        return True

    async def _record_halt(self, request: StopRequest, halt_path: str) -> None:
        """Writes the halt marker, and alerts when it cannot be written since a relaunch would then trade again."""
        if await self._attempt("halt marker", lambda: self._write_halt_marker(request, halt_path)):
            return
        await self._attempt(
            "halt marker alert",
            lambda: self.notification_handler.async_send_notification(
                NotificationType.ERROR_OCCURRED,
                error_details=(
                    f"The halt marker could not be written to {halt_path}. A supervisor relaunch "
                    "would trade again, so stop the container with `docker compose down bot`."
                ),
            ),
        )

    def _write_halt_marker(self, request: StopRequest, halt_path: str) -> None:
        record = HaltRecord(
            kind=request.kind.value,
            reason=request.reason,
            pair=self.trading_pair,
            halted_at=datetime.now(UTC).isoformat(),
        )
        write_halt_marker(halt_path, record)

    async def _notify_stop(self, request: StopRequest) -> None:
        if request.kind.halts:
            await self.notification_handler.async_send_notification(
                NotificationType.BOT_HALTED,
                kind=request.kind.value,
                reason=request.reason,
                config_path=self.config_path,
            )
        elif request.kind is StopKind.START_REFUSED:
            await self.notification_handler.async_send_notification(
                NotificationType.START_REFUSED, reason=request.reason, config_path=self.config_path
            )
        elif request.kind is StopKind.EXCHANGE_UNAVAILABLE:
            await self.notification_handler.async_send_notification(
                NotificationType.START_DEFERRED, reason=request.reason
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
        if self.reconciliation_service:
            self.reconciliation_service.cleanup()

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
            health_status["background_loops"] = self._check_background_loops()

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

    def _check_background_loops(self) -> bool:
        """False when a started background loop ended without being cancelled or is in a failure streak; a loop
        cancelled by a shutdown is ignored."""
        states = [
            self.order_status_tracker.loop_state,
            self.reconciliation_service.loop_state if self.reconciliation_service else None,
            self.state_persistence_service.checkpoint_loop_state if self.state_persistence_service else None,
        ]
        return all(
            state.running and state.consecutive_failures == 0
            for state in states
            if state is not None and not state.cancelled
        )

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
