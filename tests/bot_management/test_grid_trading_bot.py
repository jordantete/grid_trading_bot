import asyncio
import json
import os
import time
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.stop_request import StopKind, StopRequest
from grid_trading_bot.core.domain.strategy_type import StrategyType
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.persistence.run_files import RunFiles
from grid_trading_bot.core.persistence.state_recovery_service import RecoveryResult
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    UnsupportedExchangeError,
    UnsupportedTimeframeError,
)
from grid_trading_bot.core.services.market_constraints import MarketConstraints


@pytest.fixture(autouse=True)
def mock_env_vars():
    with patch.dict(os.environ, {"EXCHANGE_API_KEY": "test_api_key", "EXCHANGE_SECRET_KEY": "test_secret_key"}):
        yield


class BotFixtures:
    @pytest.fixture
    def config_manager(self):
        mock_config = Mock(spec=ConfigManager)
        mock_config.get_trading_mode.return_value = TradingMode.LIVE
        mock_config.get_initial_balance.return_value = 1000
        mock_config.get_exchange_name.return_value = "binance"
        mock_config.get_spacing_type.return_value = "arithmetic"
        mock_config.get_top_range.return_value = 2000
        mock_config.get_bottom_range.return_value = 1500
        mock_config.get_num_grids.return_value = 10
        mock_config.get_strategy_type.return_value = StrategyType.SIMPLE_GRID
        mock_config.is_persistence_enabled.return_value = False
        mock_config.get_grid_settings.return_value = {}
        mock_config.get_pair.return_value = {}
        mock_config.get_dynamic_spacing.return_value = {}
        mock_config.get_risk_management.return_value = {}
        mock_config.get_timeframe.return_value = "1h"
        return mock_config

    @pytest.fixture
    def mock_event_bus(self):
        event_bus = Mock(spec=EventBus)
        event_bus.subscribe = Mock()
        event_bus.publish_sync = Mock()
        return event_bus

    @pytest.fixture
    def notification_handler(self):
        return Mock(spec=NotificationHandler)

    @pytest.fixture
    def bot(self, config_manager, notification_handler, mock_event_bus):
        return GridTradingBot(
            config_path="config.json",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=mock_event_bus,
            save_performance_results_path="results.json",
            no_plot=True,
        )


class TestGridTradingBot(BotFixtures):
    @patch(
        "grid_trading_bot.core.bot_management.grid_trading_bot.ExchangeServiceFactory.create_exchange_service",
        side_effect=UnsupportedExchangeError("Unsupported Exchange"),
    )
    def test_initialization_with_unsupported_exchange_error(
        self,
        mock_exchange_service,
        config_manager,
        notification_handler,
        mock_event_bus,
    ):
        with patch("grid_trading_bot.core.bot_management.grid_trading_bot.logging.getLogger") as mock_logger:
            logger_instance = Mock()
            mock_logger.return_value = logger_instance

            with pytest.raises(UnsupportedExchangeError, match="Unsupported Exchange"):
                GridTradingBot("config.json", config_manager, notification_handler, mock_event_bus)

            logger_instance.error.assert_called_once_with("UnsupportedExchangeError: Unsupported Exchange")

    @patch(
        "grid_trading_bot.core.bot_management.grid_trading_bot.ExchangeServiceFactory.create_exchange_service",
        side_effect=DataFetchError("Data Fetch Error"),
    )
    def test_initialization_with_data_fetch_error(
        self,
        mock_exchange_service,
        config_manager,
        notification_handler,
        mock_event_bus,
    ):
        with patch("grid_trading_bot.core.bot_management.grid_trading_bot.logging.getLogger") as mock_logger:
            logger_instance = Mock()
            mock_logger.return_value = logger_instance

            with pytest.raises(DataFetchError, match="Data Fetch Error"):
                GridTradingBot("config.json", config_manager, notification_handler, mock_event_bus)

            logger_instance.error.assert_called_once_with("DataFetchError: Data Fetch Error")

    @patch(
        "grid_trading_bot.core.bot_management.grid_trading_bot.ExchangeServiceFactory.create_exchange_service",
        side_effect=UnsupportedTimeframeError("Unsupported Timeframe"),
    )
    def test_initialization_with_unsupported_timeframe_error(
        self,
        mock_exchange_service,
        config_manager,
        notification_handler,
        mock_event_bus,
    ):
        with patch("grid_trading_bot.core.bot_management.grid_trading_bot.logging.getLogger") as mock_logger:
            logger_instance = Mock()
            mock_logger.return_value = logger_instance

            with pytest.raises(UnsupportedTimeframeError, match="Unsupported Timeframe"):
                GridTradingBot("config.json", config_manager, notification_handler, mock_event_bus)

            logger_instance.error.assert_called_once_with("UnsupportedTimeframeError: Unsupported Timeframe")

    @patch(
        "grid_trading_bot.core.bot_management.grid_trading_bot.ExchangeServiceFactory.create_exchange_service",
        side_effect=Exception("Unexpected Error"),
    )
    def test_initialization_with_unexpected_exception(
        self,
        mock_exchange_service,
        config_manager,
        notification_handler,
        mock_event_bus,
    ):
        with patch("grid_trading_bot.core.bot_management.grid_trading_bot.logging.getLogger") as mock_logger:
            logger_instance = Mock()
            mock_logger.return_value = logger_instance

            with pytest.raises(Exception, match="Unexpected Error"):
                GridTradingBot("config.json", config_manager, notification_handler, mock_event_bus)

            logger_instance.error.assert_any_call("An unexpected error occurred.")
            logger_instance.error.assert_any_call(ANY)

    def test_initialization_with_missing_config(self, notification_handler, mock_event_bus):
        config_manager = Mock(spec=ConfigManager)
        config_manager.get_trading_mode.side_effect = AttributeError("Missing configuration")

        with patch("grid_trading_bot.core.bot_management.grid_trading_bot.logging.getLogger") as mock_logger:
            logger_instance = Mock()
            mock_logger.return_value = logger_instance

            with pytest.raises(AttributeError, match="Missing configuration"):
                GridTradingBot("config.json", config_manager, notification_handler, mock_event_bus)

            logger_instance.error.assert_any_call("An unexpected error occurred.")
            logger_instance.error.assert_any_call(ANY)

    @patch("grid_trading_bot.core.bot_management.grid_trading_bot.GridTradingStrategy")
    @pytest.mark.asyncio
    async def test_run_loads_markets_and_sets_constraints_in_live_mode(self, mock_strategy, bot):
        bot.balance_tracker = Mock()
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.run = AsyncMock()
        bot.strategy.grid_manager = Mock()
        bot.exchange_service.load_exchange_markets = AsyncMock()
        constraints = MarketConstraints(min_amount=0.001, min_cost=10.0)
        bot.exchange_service.get_market_constraints = Mock(return_value=constraints)
        bot.order_manager.set_market_constraints = Mock()
        bot.reconciliation_service = Mock()
        bot.exchange_service.close = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()

        await bot.run()

        bot.exchange_service.load_exchange_markets.assert_awaited_once_with(bot.trading_pair)
        bot.exchange_service.get_market_constraints.assert_called_once_with(bot.trading_pair)
        bot.order_manager.set_market_constraints.assert_called_once_with(constraints)
        bot.reconciliation_service.set_market_constraints.assert_called_once_with(constraints)

    @pytest.mark.asyncio
    async def test_get_bot_health_status(self, bot):
        bot._check_strategy_health = AsyncMock(return_value=True)
        bot._get_exchange_status = AsyncMock(return_value="ok")

        health_status = await bot.get_bot_health_status()

        assert health_status["strategy"] is True
        assert health_status["exchange_status"] == "ok"
        assert health_status["overall"] is True

    @pytest.mark.asyncio
    async def test_is_healthy_strategy_stopped(self, bot):
        bot.strategy = Mock()
        bot.strategy._running = False
        bot.exchange_service.get_exchange_status = AsyncMock(return_value={"status": "ok"})

        health_status = await bot.get_bot_health_status()

        assert health_status["strategy"] is False
        assert health_status["exchange_status"] == "ok"
        assert health_status["overall"] is False

    @pytest.mark.asyncio
    async def test_health_status_flags_stale_ticker_feed(self, bot):
        bot._check_strategy_health = AsyncMock(return_value=True)
        bot._get_exchange_status = AsyncMock(return_value="ok")
        bot.exchange_service.last_price_update_ts = time.monotonic() - 500

        health_status = await bot.get_bot_health_status()

        assert health_status["ticker_feed"] is False
        assert health_status["overall"] is False

    @pytest.mark.asyncio
    async def test_health_status_fresh_ticker_feed_ok(self, bot):
        bot._check_strategy_health = AsyncMock(return_value=True)
        bot._get_exchange_status = AsyncMock(return_value="ok")
        bot.exchange_service.last_price_update_ts = time.monotonic()

        health_status = await bot.get_bot_health_status()

        assert health_status["ticker_feed"] is True
        assert health_status["overall"] is True

    @patch("grid_trading_bot.core.bot_management.grid_trading_bot.GridTradingBot._generate_and_log_performance")
    @pytest.mark.asyncio
    async def test_generate_and_log_performance_direct(self, mock_performance, bot):
        mock_performance.return_value = {
            "config": bot.config_path,
            "performance_summary": {"profit": 100},
            "orders": [],
        }

        result = bot._generate_and_log_performance()

        assert result == {
            "config": bot.config_path,
            "performance_summary": {"profit": 100},
            "orders": [],
        }

    def test_generate_and_log_performance_exposes_typed_metrics(self, bot):
        metrics = Mock()
        bot.strategy.generate_performance_report = Mock(return_value=(metrics, {"ROI": "1.00%"}, []))

        result = bot._generate_and_log_performance()

        assert result == {
            "config": bot.config_path,
            "performance_summary": {"ROI": "1.00%"},
            "orders": [],
            "metrics": metrics,
        }

    @pytest.mark.asyncio
    async def test_get_exchange_status(self, bot):
        bot.exchange_service = MagicMock()
        bot.exchange_service.get_exchange_status = AsyncMock(return_value={"status": "ok"})

        result = await bot._get_exchange_status()
        assert result == "ok"

    def test_get_balance_zero_values(self, bot):
        bot.balance_tracker = Mock()
        bot.balance_tracker.balance = 0.0
        bot.balance_tracker.reserved_fiat = 0.0
        bot.balance_tracker.crypto_balance = 0.0
        bot.balance_tracker.reserved_crypto = 0.0

        result = bot.get_balances()

        assert result == {
            "fiat": 0.0,
            "crypto": 0.0,
            "reserved_fiat": 0.0,
            "reserved_crypto": 0.0,
        }

    def test_get_account_snapshot_values_the_account_at_the_last_accepted_price(self, bot):
        bot.exchange_service = Mock(last_price=61250.0)
        bot.balance_tracker = Mock()

        assert bot.get_account_snapshot() is bot.balance_tracker.snapshot.return_value
        bot.balance_tracker.snapshot.assert_called_once_with(61250.0)

    def test_get_account_snapshot_is_none_before_the_first_price(self, bot):
        bot.exchange_service = Mock(last_price=None)
        bot.balance_tracker = Mock()

        assert bot.get_account_snapshot() is None
        bot.balance_tracker.snapshot.assert_not_called()

    def test_get_account_snapshot_is_none_without_a_price_feed(self, bot):
        bot.exchange_service = Mock(spec=[])
        bot.balance_tracker = Mock()

        assert bot.get_account_snapshot() is None

    @patch("grid_trading_bot.core.bot_management.grid_trading_bot.GridTradingStrategy")
    @pytest.mark.asyncio
    async def test_run_strategy_with_exception(self, mock_strategy, bot):
        # Arrange: Mock dependencies
        bot.balance_tracker = Mock()
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(
            return_value=MarketConstraints(min_amount=None, min_cost=None),
        )
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.run = AsyncMock(side_effect=Exception("Test Exception"))
        mock_grid_manager = Mock()
        mock_grid_manager.initialize_grids_and_levels = Mock()
        bot.strategy.grid_manager = mock_grid_manager
        bot.exchange_service.close = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()

        with patch.object(bot.logger, "error") as mock_logger_error:
            # Act and Assert: Ensure the bot logs the error and re-raises the exception
            with pytest.raises(Exception, match="Test Exception"):
                await bot.run()

            # Ensure the error is logged correctly
            mock_logger_error.assert_any_call("An unexpected error occurred Test Exception")
            assert bot.is_running is False

    @patch("grid_trading_bot.core.bot_management.grid_trading_bot.OrderStatusTracker")
    @pytest.mark.asyncio
    async def test_stop_bot(self, mock_order_status_tracker, bot):
        bot.is_running = True
        bot.strategy.stop = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()
        bot.exchange_service.close = AsyncMock()

        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        bot.strategy.stop.assert_awaited_once()
        bot.order_status_tracker.stop_tracking.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_stop_bot_event(self, bot):
        bot.is_running = True
        bot._stop = AsyncMock()
        request = StopRequest(StopKind.MANUAL, "Test reason")

        await bot._handle_stop_bot_event(request)

        bot._stop.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_handle_stop_bot_event_forwards_the_request_even_when_not_running(self, bot):
        bot.is_running = False
        bot._stop = AsyncMock()
        request = StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15)

        await bot._handle_stop_bot_event(request)

        assert bot._stop.await_args.args[0] is request

    @patch("grid_trading_bot.core.bot_management.grid_trading_bot.GridTradingStrategy")
    @pytest.mark.asyncio
    async def test_run_with_plotting_enabled(self, mock_strategy, bot):
        bot.no_plot = False
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(
            return_value=MarketConstraints(min_amount=None, min_cost=None),
        )
        bot._generate_and_log_performance = Mock()
        bot.strategy.plot_results = Mock()
        bot.strategy.run = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        mock_grid_manager = Mock()
        mock_grid_manager.initialize_grids_and_levels = Mock()
        bot.strategy.grid_manager = mock_grid_manager
        bot.exchange_service.close = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()

        await bot.run()

        bot.strategy.plot_results.assert_called_once()
        bot.strategy.run.assert_awaited_once()
        bot.order_status_tracker.start_tracking.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_orders_tracker_before_strategy(self, bot):
        call_order = []
        bot.order_status_tracker.stop_tracking = AsyncMock(side_effect=lambda: call_order.append("tracker"))
        bot.strategy.stop = AsyncMock(side_effect=lambda: call_order.append("strategy"))
        bot.exchange_service.close = AsyncMock()
        bot.is_running = True

        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        assert call_order == ["tracker", "strategy"]


class TestStrategyStatePersistenceWiring:
    @pytest.fixture
    def config_manager(self, tmp_path):
        mock_config = Mock(spec=ConfigManager)
        mock_config.get_trading_mode.return_value = TradingMode.LIVE
        mock_config.get_initial_balance.return_value = 1000
        mock_config.get_exchange_name.return_value = "binance"
        mock_config.get_spacing_type.return_value = "arithmetic"
        mock_config.get_top_range.return_value = 2000
        mock_config.get_bottom_range.return_value = 1500
        mock_config.get_num_grids.return_value = 10
        mock_config.get_strategy_type.return_value = StrategyType.SIMPLE_GRID
        mock_config.is_persistence_enabled.return_value = True
        mock_config.get_state_db_path.return_value = str(tmp_path / "state.db")
        mock_config.get_checkpoint_interval_seconds.return_value = 60.0
        mock_config.get_grid_settings.return_value = {
            "type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": 10,
            "range": [1500, 2000],
        }
        mock_config.get_pair.return_value = "BTC/USDT"
        mock_config.get_dynamic_spacing.return_value = {}
        mock_config.get_risk_management.return_value = {}
        mock_config.get_timeframe.return_value = "1h"
        return mock_config

    @pytest.fixture
    def mock_event_bus(self):
        event_bus = Mock(spec=EventBus)
        event_bus.subscribe = Mock()
        event_bus.publish_sync = Mock()
        return event_bus

    @pytest.fixture
    def notification_handler(self):
        return Mock(spec=NotificationHandler)

    @pytest.fixture
    def bot(self, config_manager, notification_handler, mock_event_bus):
        with patch("grid_trading_bot.core.bot_management.grid_trading_bot.SQLiteStateRepository") as mock_repo_cls:
            mock_repo_cls.return_value = MagicMock()
            yield GridTradingBot(
                config_path="config.json",
                config_manager=config_manager,
                notification_handler=notification_handler,
                event_bus=mock_event_bus,
                save_performance_results_path="results.json",
                no_plot=True,
            )

    def test_persistence_service_wired_with_strategy_provider(self, bot):
        assert bot.state_persistence_service is not None
        assert bot.state_persistence_service.strategy_state_provider == bot.strategy.export_strategy_state

    @pytest.mark.asyncio
    async def test_run_restores_strategy_state_after_recovery(self, bot):
        bot.strategy.grid_manager = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot.strategy.restore_strategy_state = Mock()
        bot.order_status_tracker.start_tracking = Mock()
        bot._generate_and_log_performance = Mock(return_value={})
        bot.reconciliation_service = None
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(
            return_value=MarketConstraints(min_amount=None, min_cost=None),
        )
        bot.exchange_service.close = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()
        bot.state_persistence_service.start_periodic_checkpoints = Mock()
        bot.order_manager.checkpoint = AsyncMock()
        bot.state_persistence_service.cleanup = Mock()

        recovery_result = RecoveryResult(
            recovered=True,
            initial_purchase_done=True,
            grid_orders_initialized=True,
            strategy_state={"trailing_stop": None, "atr_grid": 3.5},
        )
        bot.state_recovery_service.attempt_recovery = AsyncMock(return_value=recovery_result)

        await bot.run()

        bot.strategy.restore_strategy_state.assert_called_once_with({"trailing_stop": None, "atr_grid": 3.5})
        bot.strategy.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_skips_restore_when_no_strategy_state(self, bot):
        bot.strategy.grid_manager = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot.strategy.restore_strategy_state = Mock()
        bot.order_status_tracker.start_tracking = Mock()
        bot._generate_and_log_performance = Mock(return_value={})
        bot.reconciliation_service = None
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(
            return_value=MarketConstraints(min_amount=None, min_cost=None),
        )
        bot.exchange_service.close = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()
        bot.state_persistence_service.start_periodic_checkpoints = Mock()
        bot.order_manager.checkpoint = AsyncMock()
        bot.state_persistence_service.cleanup = Mock()

        recovery_result = RecoveryResult(
            recovered=True,
            initial_purchase_done=False,
            grid_orders_initialized=False,
            strategy_state=None,
        )
        bot.state_recovery_service.attempt_recovery = AsyncMock(return_value=recovery_result)

        await bot.run()

        bot.strategy.restore_strategy_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_recovery_resets_memory_state(self, bot):
        bot.grid_manager = Mock()
        bot.order_book = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot._generate_and_log_performance = Mock(return_value={})
        bot.reconciliation_service = None
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(
            return_value=MarketConstraints(min_amount=None, min_cost=None),
        )
        bot.exchange_service.close = AsyncMock()
        bot.order_status_tracker.stop_tracking = AsyncMock()
        bot.state_persistence_service.start_periodic_checkpoints = Mock()
        bot.order_manager.checkpoint = AsyncMock()
        bot.state_persistence_service.cleanup = Mock()

        bot.state_recovery_service.attempt_recovery = AsyncMock(
            return_value=RecoveryResult(recovered=False, errors=["boom"])
        )

        await bot.run()

        bot.grid_manager.reset.assert_called_once()
        bot.order_book.clear.assert_called_once()
        assert bot.strategy.initialize_strategy.call_count == 2  # once pre-recovery, once post-reset
        bot.balance_tracker.setup_balances.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_checkpoints_before_cleanup(self, bot):
        bot.is_running = True
        bot._state_ready = True
        bot.order_status_tracker.stop_tracking = AsyncMock()
        bot.reconciliation_service = None
        call_order = []
        bot.strategy.stop = AsyncMock(side_effect=lambda: call_order.append("strategy"))
        bot.order_manager.checkpoint = AsyncMock(side_effect=lambda **_kw: call_order.append("checkpoint"))
        bot.state_persistence_service.cleanup = Mock(side_effect=lambda: call_order.append("cleanup"))
        bot.exchange_service.close = AsyncMock()

        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        assert call_order == ["strategy", "checkpoint", "cleanup"]

    def test_order_manager_saves_through_the_persistence_service(self, bot):
        assert bot.order_manager.state_store is bot.state_persistence_service

    @pytest.mark.asyncio
    async def test_recovered_flags_reach_the_order_manager(self, bot):
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.order_status_tracker.stop_tracking = AsyncMock()
        bot._generate_and_log_performance = Mock(return_value={})
        bot.reconciliation_service = None
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.exchange_service.close = AsyncMock()
        bot.state_persistence_service.start_periodic_checkpoints = Mock()
        bot.state_persistence_service.cleanup = Mock()
        bot.order_manager.checkpoint = AsyncMock()
        bot.state_recovery_service.attempt_recovery = AsyncMock(
            return_value=RecoveryResult(recovered=True, initial_purchase_done=True, grid_orders_initialized=False)
        )

        await bot.run()

        assert bot.order_manager.arming == ArmingFlags(initial_purchase_done=True, grid_orders_initialized=False)
        bot.state_persistence_service.start_periodic_checkpoints.assert_called_once_with(
            60.0, bot.order_manager.checkpoint
        )


def _record_shutdown(bot, calls, run_files=None):
    bot.reconciliation_service = None
    bot.order_status_tracker.stop_tracking = AsyncMock(side_effect=lambda: calls.append("tracking"))
    bot.strategy.stop = AsyncMock(side_effect=lambda: calls.append("strategy"))
    bot.order_manager.close_positions = AsyncMock(side_effect=lambda **_kw: calls.append("cancel"))
    bot.state_persistence_service = Mock()
    bot.order_manager.checkpoint = AsyncMock(side_effect=lambda **_kw: calls.append("checkpoint"))
    bot.notification_handler.async_send_notification = AsyncMock(side_effect=lambda *_a, **_kw: calls.append("notify"))
    bot.exchange_service.close = AsyncMock(side_effect=lambda: calls.append("close"))
    bot.run_files = run_files
    bot.is_running = True
    bot._state_ready = True
    return bot


def _run_files(tmp_path):
    return RunFiles(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "bot_x.lock"),
        halt_path=str(tmp_path / "halted_x.json"),
    )


class TestShutdownSequence(BotFixtures):
    @pytest.mark.asyncio
    async def test_a_halt_marks_cancels_checkpoints_notifies_and_closes_last(self, bot, tmp_path):
        calls = []
        files = _run_files(tmp_path)
        _record_shutdown(bot, calls, files)

        await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        assert calls == ["tracking", "strategy", "cancel", "checkpoint", "notify", "close"]
        bot.order_manager.close_positions.assert_awaited_once_with(liquidate=False)
        marker = json.loads((tmp_path / "halted_x.json").read_text())
        assert marker["kind"] == "manual"
        assert marker["reason"] == "User issued stop command"
        assert bot.notification_handler.async_send_notification.await_args.args[0] == NotificationType.BOT_HALTED

    @pytest.mark.asyncio
    async def test_the_final_checkpoint_bounds_its_wait_for_the_lock(self, bot):
        _record_shutdown(bot, [])

        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        bot.order_manager.checkpoint.assert_awaited_once_with(lock_timeout=GridTradingBot.CHECKPOINT_LOCK_TIMEOUT)

    @pytest.mark.asyncio
    async def test_the_halt_marker_exists_before_the_orders_are_cancelled(self, bot, tmp_path):
        calls = []
        files = _run_files(tmp_path)
        _record_shutdown(bot, calls, files)
        marker_present_at_cancel = []
        bot.order_manager.close_positions = AsyncMock(
            side_effect=lambda **_kw: marker_present_at_cancel.append(os.path.exists(files.halt_path))
        )

        await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        assert marker_present_at_cancel == [True]

    @pytest.mark.asyncio
    async def test_a_stop_waits_for_the_ticker_update_once_the_strategy_is_stopped(self, bot, tmp_path):
        calls = []
        _record_shutdown(bot, calls, _run_files(tmp_path))
        bot.strategy.wait_for_tick_to_finish = AsyncMock(side_effect=lambda timeout: calls.append(f"tick {timeout}"))

        await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        assert calls == ["tracking", "strategy", "tick 20.0", "cancel", "checkpoint", "notify", "close"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "request_",
        [
            StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15),
            StopRequest(StopKind.FEED_LOST, "feed died"),
            StopRequest(StopKind.CRASH, "boom"),
        ],
    )
    async def test_a_suspend_keeps_the_orders(self, request_, bot, tmp_path):
        calls = []
        _record_shutdown(bot, calls, _run_files(tmp_path))

        await bot._stop(request_)

        assert calls == ["tracking", "strategy", "checkpoint", "notify", "close"]
        assert not (tmp_path / "halted_x.json").exists()
        assert bot.notification_handler.async_send_notification.await_args.args[0] == NotificationType.BOT_SUSPENDED

    @pytest.mark.asyncio
    async def test_a_second_request_is_ignored(self, bot, tmp_path):
        calls = []
        _record_shutdown(bot, calls, _run_files(tmp_path))

        await bot._stop(StopRequest(StopKind.RISK, "TP or SL hit."))
        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        assert bot.stop_request.kind is StopKind.RISK
        assert calls.count("close") == 1
        assert (tmp_path / "halted_x.json").exists()

    @pytest.mark.asyncio
    async def test_failed_cancellation_still_checkpoints_marks_and_closes(self, bot, tmp_path):
        calls = []
        _record_shutdown(bot, calls, _run_files(tmp_path))
        bot.order_manager.close_positions = AsyncMock(side_effect=Exception("exchange down"))

        await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        assert calls == ["tracking", "strategy", "checkpoint", "notify", "close"]
        assert (tmp_path / "halted_x.json").exists()

    @pytest.mark.asyncio
    async def test_a_failed_persistence_cleanup_still_marks_notifies_and_closes(self, bot, tmp_path):
        calls = []
        _record_shutdown(bot, calls, _run_files(tmp_path))
        bot.state_persistence_service.cleanup = Mock(side_effect=Exception("database is locked"))

        await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        assert calls == ["tracking", "strategy", "cancel", "checkpoint", "notify", "close"]
        assert (tmp_path / "halted_x.json").exists()

    @pytest.mark.asyncio
    async def test_a_stop_before_the_state_is_loaded_keeps_the_saved_state(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False

        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        assert calls == ["tracking", "strategy", "notify", "close"]
        bot.order_manager.checkpoint.assert_not_awaited()
        bot.state_persistence_service.cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_startup_crash_keeps_the_saved_state(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False
        bot.exchange_service.load_exchange_markets = AsyncMock(side_effect=ConnectionError("exchange unreachable"))

        with pytest.raises(ConnectionError, match="exchange unreachable"):
            await bot.run()

        assert bot.stop_request.kind is StopKind.CRASH
        bot.order_manager.checkpoint.assert_not_awaited()
        bot.state_persistence_service.cleanup.assert_called_once()
        assert calls[-1] == "close"

    @pytest.mark.asyncio
    async def test_a_crash_once_setup_completed_checkpoints(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock(side_effect=RuntimeError("bug"))

        with pytest.raises(RuntimeError, match="bug"):
            await bot.run()

        assert calls.index("checkpoint") < calls.index("close")

    @pytest.mark.asyncio
    async def test_a_stop_during_recovery_runs_once_recovery_returns_and_checkpoints_the_recovered_state(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False
        bot.order_manager.cleanup = Mock(side_effect=lambda: calls.append("order manager"))
        recovery_started = asyncio.Event()
        recovery_may_return = asyncio.Event()

        async def slow_recovery():
            recovery_started.set()
            await recovery_may_return.wait()
            calls.append("recovered")
            return RecoveryResult(recovered=True, initial_purchase_done=True, grid_orders_initialized=True)

        bot.state_recovery_service = Mock()
        bot.state_recovery_service.attempt_recovery = AsyncMock(side_effect=slow_recovery)
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot._generate_and_log_performance = Mock(return_value={})

        run_task = asyncio.create_task(bot.run())
        await asyncio.wait_for(recovery_started.wait(), timeout=1)
        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        assert calls == []
        recovery_may_return.set()
        await asyncio.wait_for(run_task, timeout=1)

        assert calls == ["recovered", "tracking", "strategy", "checkpoint", "notify", "order manager", "close"]
        bot.strategy.run.assert_not_awaited()
        bot.order_status_tracker.start_tracking.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stop_during_startup_then_a_startup_error_shuts_down_once(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False
        calls_when_requested = []

        async def markets_fail(_pair):
            await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))
            calls_when_requested.extend(calls)
            raise ConnectionError("exchange unreachable")

        bot.exchange_service.load_exchange_markets = AsyncMock(side_effect=markets_fail)

        with pytest.raises(ConnectionError, match="exchange unreachable"):
            await asyncio.wait_for(bot.run(), timeout=1)

        assert calls_when_requested == []
        assert calls == ["tracking", "strategy", "notify", "close"]
        assert bot.stop_request.kind is StopKind.SIGNAL
        bot.order_manager.checkpoint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_halt_marker_alerts_then_still_cancels_checkpoints_and_closes(self, bot, tmp_path):
        calls = []
        files = _run_files(tmp_path)
        _record_shutdown(bot, calls, files)

        with patch(
            "grid_trading_bot.core.bot_management.grid_trading_bot.write_halt_marker",
            side_effect=OSError("read-only file system"),
        ):
            await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        assert calls == ["tracking", "strategy", "notify", "cancel", "checkpoint", "notify", "close"]
        sent = bot.notification_handler.async_send_notification.await_args_list
        assert sent[0].args[0] == NotificationType.ERROR_OCCURRED
        details = sent[0].kwargs["error_details"]
        assert files.halt_path in details
        assert "relaunch would trade again" in details
        assert "docker compose down" in details
        assert sent[1].args[0] == NotificationType.BOT_HALTED

    @pytest.mark.asyncio
    async def test_the_halt_notification_asks_to_check_the_exchange_before_resuming(self, bot):
        calls = []
        _record_shutdown(bot, calls)

        await bot._stop(StopRequest(StopKind.MANUAL, "User issued stop command"))

        sent = bot.notification_handler.async_send_notification.await_args
        assert sent.args[0].value.message.format(**sent.kwargs) == (
            "Trading halted (manual): User issued stop command\n"
            "The bot will not trade again until `grid_trading_bot resume --config config.json` is run. "
            "Check the exchange for orders left open before resuming."
        )

    @pytest.mark.asyncio
    async def test_a_stop_during_paired_placement_runs_once_the_pairs_are_placed_and_starts_nothing(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False
        placing = asyncio.Event()
        may_finish = asyncio.Event()

        async def slow_place(_fills):
            placing.set()
            await may_finish.wait()
            calls.append("paired placed")

        bot.order_manager.place_paired_orders_for_recovered_fills = AsyncMock(side_effect=slow_place)
        bot.state_recovery_service = Mock()
        bot.state_recovery_service.attempt_recovery = AsyncMock(
            return_value=RecoveryResult(
                recovered=True,
                initial_purchase_done=True,
                grid_orders_initialized=True,
                orders_filled_while_down=[("order", "level")],
            )
        )
        bot.state_persistence_service.start_periodic_checkpoints = Mock(
            side_effect=lambda *_a: calls.append("periodic checkpoints START")
        )
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.order_status_tracker.start_tracking = Mock(side_effect=lambda: calls.append("tracking START"))
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock(side_effect=lambda **_k: calls.append("strategy.run ENTERED"))
        bot._generate_and_log_performance = Mock(return_value={})

        task = asyncio.create_task(bot.run())
        await asyncio.wait_for(placing.wait(), timeout=1)
        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))
        calls.append("<_stop returned>")
        may_finish.set()
        await asyncio.wait_for(task, timeout=1)

        assert calls == [
            "<_stop returned>",
            "paired placed",
            "tracking",
            "strategy",
            "checkpoint",
            "notify",
            "close",
        ]

    @pytest.mark.asyncio
    async def test_a_stop_during_recovery_still_places_the_recovered_pairs_before_the_checkpoint(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot._state_ready = False
        recovery_started = asyncio.Event()
        recovery_may_return = asyncio.Event()

        async def slow_recovery():
            recovery_started.set()
            await recovery_may_return.wait()
            return RecoveryResult(
                recovered=True,
                initial_purchase_done=True,
                grid_orders_initialized=True,
                orders_filled_while_down=[("order", "level")],
            )

        bot.state_recovery_service = Mock()
        bot.state_recovery_service.attempt_recovery = AsyncMock(side_effect=slow_recovery)
        bot.order_manager.place_paired_orders_for_recovered_fills = AsyncMock(
            side_effect=lambda _fills: calls.append("paired placed")
        )
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot._generate_and_log_performance = Mock(return_value={})

        task = asyncio.create_task(bot.run())
        await asyncio.wait_for(recovery_started.wait(), timeout=1)
        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))
        recovery_may_return.set()
        await asyncio.wait_for(task, timeout=1)

        assert calls == ["paired placed", "tracking", "strategy", "checkpoint", "notify", "close"]
        bot.strategy.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_risk_stop_published_by_the_strategy_through_a_real_event_bus_shuts_down_once(
        self, config_manager, notification_handler
    ):
        bot = GridTradingBot(
            config_path="config.json",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=EventBus(),
            no_plot=True,
        )
        calls = []
        _record_shutdown(bot, calls)
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot._generate_and_log_performance = Mock(return_value={})

        async def strategy_run(**_kwargs):
            await bot.event_bus.publish(Events.STOP_BOT, StopRequest(StopKind.RISK, "TP or SL hit."))

        bot.strategy.run = AsyncMock(side_effect=strategy_run)

        await asyncio.wait_for(bot.run(), timeout=1)

        assert bot.stop_request.kind is StopKind.RISK
        assert calls == ["tracking", "strategy", "cancel", "checkpoint", "notify", "close"]
        bot.exchange_service.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_backtest_halt_writes_no_marker_and_sends_nothing(self, bot, tmp_path):
        calls = []
        _record_shutdown(bot, calls, _run_files(tmp_path))
        bot.trading_mode = TradingMode.BACKTEST

        await bot._stop(StopRequest(StopKind.RISK, "TP or SL hit."))

        assert calls == ["tracking", "strategy", "checkpoint", "close"]
        assert not (tmp_path / "halted_x.json").exists()

    @pytest.mark.asyncio
    async def test_request_stop_publishes_stop_bot(self, bot):
        request = StopRequest(StopKind.MANUAL, "Received SIGUSR1")
        bot.event_bus.publish = AsyncMock()

        await bot.request_stop(request)

        bot.event_bus.publish.assert_awaited_once_with(Events.STOP_BOT, request)

    @pytest.mark.asyncio
    async def test_a_stop_before_run_skips_startup(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.run = AsyncMock()
        bot.strategy.initialize_strategy = Mock()
        bot._generate_and_log_performance = Mock(return_value={})
        await bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))

        await bot.run()

        bot.exchange_service.load_exchange_markets.assert_not_awaited()
        bot.strategy.run.assert_not_awaited()
        bot.order_status_tracker.start_tracking.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_waits_for_the_stop_to_finish(self, bot):
        calls = []
        _record_shutdown(bot, calls)

        async def slow_checkpoint(**_kw):
            await asyncio.sleep(0.05)
            calls.append("checkpoint")

        bot.order_manager.checkpoint = AsyncMock(side_effect=slow_checkpoint)
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot._generate_and_log_performance = Mock(return_value={})

        async def strategy_run(**_kwargs):
            asyncio.get_running_loop().create_task(
                bot._stop(StopRequest(StopKind.SIGNAL, "Received SIGTERM", signal_number=15))
            )
            await asyncio.sleep(0)

        bot.strategy.run = AsyncMock(side_effect=strategy_run)

        await bot.run()

        assert "close" in calls
        assert calls.index("checkpoint") < calls.index("close")

    @pytest.mark.asyncio
    async def test_a_crash_stops_with_the_crash_kind_and_reraises(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock(side_effect=RuntimeError("bug"))

        with pytest.raises(RuntimeError, match="bug"):
            await bot.run()

        assert bot.stop_request.kind is StopKind.CRASH
        assert calls[-1] == "close"

    @pytest.mark.asyncio
    async def test_a_live_loop_ending_without_a_request_counts_as_a_crash(self, bot):
        calls = []
        _record_shutdown(bot, calls)
        bot.exchange_service.load_exchange_markets = AsyncMock()
        bot.exchange_service.get_market_constraints = Mock(return_value=MarketConstraints(None, None))
        bot.balance_tracker.setup_balances = AsyncMock()
        bot.order_status_tracker.start_tracking = Mock()
        bot.strategy.initialize_strategy = Mock()
        bot.strategy.run = AsyncMock()
        bot._generate_and_log_performance = Mock(return_value={})

        await bot.run()

        assert bot.stop_request.kind is StopKind.CRASH
