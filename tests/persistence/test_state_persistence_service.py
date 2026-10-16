import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.stop_request import StopKind
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_repository_interface import StateSnapshot


@pytest.fixture
def setup_persistence_service():
    from grid_trading_bot.config.trading_mode import TradingMode

    repository = MagicMock()
    event_bus = EventBus()
    notification_handler = MagicMock()
    notification_handler.async_send_notification = AsyncMock()
    order_book = MagicMock()
    order_book.get_buy_orders_with_grid.return_value = []
    order_book.get_sell_orders_with_grid.return_value = []
    grid_manager = MagicMock()
    grid_manager.grid_levels = {}
    balance_tracker = MagicMock()
    config_manager = MagicMock()
    config_manager.get_grid_settings.return_value = {
        "type": "simple_grid",
        "spacing": "arithmetic",
        "num_grids": 10,
        "range": [50000, 60000],
    }
    config_manager.get_pair.return_value = "BTC/USDT"
    config_manager.get_dynamic_spacing.return_value = {}
    config_manager.get_exchange_name.return_value = "binance"
    config_manager.get_trading_mode.return_value = TradingMode.BACKTEST
    config_manager.get_timeframe.return_value = "1h"

    service = StatePersistenceService(
        repository=repository,
        event_bus=event_bus,
        notification_handler=notification_handler,
        order_book=order_book,
        grid_manager=grid_manager,
        balance_tracker=balance_tracker,
        config_manager=config_manager,
        trading_pair="BTC/USDT",
        strategy_type="simple_grid",
    )
    return service, repository, event_bus, order_book, grid_manager, balance_tracker, notification_handler


def _saved(repository) -> StateSnapshot:
    return repository.save_state.call_args[0][0]


class TestEventSubscription:
    def test_subscribes_to_events(self, setup_persistence_service):
        service, _, event_bus, *_ = setup_persistence_service

        assert service._on_order_filled in event_bus.subscribers[Events.ORDER_FILLED]
        assert service._on_order_cancelled in event_bus.subscribers[Events.ORDER_CANCELLED]
        assert service._on_initial_purchase_done in event_bus.subscribers[Events.INITIAL_PURCHASE_DONE]
        assert service._on_grid_orders_initialized in event_bus.subscribers[Events.GRID_ORDERS_INITIALIZED]

    def test_cleanup_unsubscribes_and_closes_the_repository(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service

        service.cleanup()

        for callbacks in event_bus.subscribers.values():
            assert service._on_order_filled not in callbacks
            assert service._on_order_cancelled not in callbacks
            assert service._on_initial_purchase_done not in callbacks
            assert service._on_grid_orders_initialized not in callbacks
        repository.close.assert_called_once()


class TestEventsSave:
    @pytest.mark.parametrize("event", [Events.ORDER_FILLED, Events.ORDER_CANCELLED])
    async def test_an_order_event_saves(self, setup_persistence_service, event):
        _service, repository, event_bus, *_ = setup_persistence_service

        await event_bus.publish(event, MagicMock())

        repository.save_state.assert_called_once()

    async def test_initial_purchase_done_sets_its_flag(self, setup_persistence_service):
        _service, repository, event_bus, *_ = setup_persistence_service

        await event_bus.publish(Events.INITIAL_PURCHASE_DONE, {})

        assert _saved(repository).bot_state["initial_purchase_done"] is True

    async def test_grid_orders_initialized_sets_its_flag(self, setup_persistence_service):
        _service, repository, event_bus, *_ = setup_persistence_service

        await event_bus.publish(Events.GRID_ORDERS_INITIALIZED, {})

        assert _saved(repository).bot_state["grid_orders_initialized"] is True

    async def test_set_flags_then_checkpoint_now(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        service.set_flags(initial_purchase_done=True, grid_orders_initialized=True)
        await service.checkpoint_now()

        assert _saved(repository).bot_state["initial_purchase_done"] is True
        assert _saved(repository).bot_state["grid_orders_initialized"] is True


class TestSnapshot:
    def test_snapshot_holds_the_flags_and_every_component(self, setup_persistence_service):
        service, *_ = setup_persistence_service

        snapshot = service.snapshot(ArmingFlags(initial_purchase_done=True))

        assert "config_hash" in snapshot.bot_state
        assert snapshot.bot_state["trading_pair"] == "BTC/USDT"
        assert snapshot.bot_state["strategy_type"] == "simple_grid"
        assert snapshot.bot_state["initial_purchase_done"] is True
        assert snapshot.bot_state["grid_orders_initialized"] is False
        assert snapshot.orders == []
        assert snapshot.grid_levels == []

    def test_snapshot_includes_the_strategy_state(self, setup_persistence_service):
        service, *_ = setup_persistence_service
        service.strategy_state_provider = lambda: {"trailing_stop": None, "atr_grid": 3.5}

        snapshot = service.snapshot(ArmingFlags())

        assert json.loads(snapshot.bot_state["strategy_state"]) == {"trailing_stop": None, "atr_grid": 3.5}

    def test_snapshot_without_provider_has_no_strategy_state(self, setup_persistence_service):
        service, *_ = setup_persistence_service

        assert service.snapshot(ArmingFlags()).bot_state["strategy_state"] is None


class TestSave:
    async def test_a_save_writes_one_snapshot(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        await service.save(ArmingFlags())

        repository.save_state.assert_called_once()
        assert isinstance(_saved(repository), StateSnapshot)

    async def test_the_first_failure_alerts_without_raising(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        repository.save_state.side_effect = OSError("disk I/O error")

        await service.save(ArmingFlags())

        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.CHECKPOINT_FAILED, error_details="disk I/O error"
        )

    async def test_a_success_after_failures_alerts_once_and_counts_again(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        repository.save_state.side_effect = [OSError("full"), OSError("full"), None, OSError("full")]

        for _ in range(4):
            await service.save(ArmingFlags())

        sent = notification_handler.async_send_notification.await_args_list
        assert [call.args[0] for call in sent] == [
            NotificationType.CHECKPOINT_FAILED,
            NotificationType.CHECKPOINT_RESTORED,
            NotificationType.CHECKPOINT_FAILED,
        ]
        assert sent[1].kwargs == {"failures": 2}

    async def test_the_third_failure_in_a_row_requests_a_persistence_halt(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service
        requests = []
        event_bus.subscribe(Events.STOP_BOT, requests.append)
        repository.save_state.side_effect = OSError("full")

        for _ in range(3):
            await service.save(ArmingFlags())
        await service._halt_task

        assert [request.kind for request in requests] == [StopKind.PERSISTENCE]
        assert "3 checkpoints failed in a row" in requests[0].reason

    async def test_failures_beyond_the_third_request_no_second_halt(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service
        requests = []
        event_bus.subscribe(Events.STOP_BOT, requests.append)
        repository.save_state.side_effect = OSError("full")

        for _ in range(5):
            await service.save(ArmingFlags())
        await service._halt_task
        await asyncio.sleep(0)

        assert len(requests) == 1

    async def test_a_snapshot_that_cannot_be_built_counts_as_a_failure(self, setup_persistence_service):
        service, repository, _, order_book, *_, notification_handler = setup_persistence_service
        order_book.get_buy_orders_with_grid.side_effect = RuntimeError("book broken")

        await service.save(ArmingFlags())

        repository.save_state.assert_not_called()
        notification_handler.async_send_notification.assert_awaited_once()


class TestPeriodicCheckpoints:
    async def test_the_loop_writes_on_its_interval(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        service.start_periodic_checkpoints(interval_seconds=0.01)
        await asyncio.sleep(0.05)
        service.cleanup()

        assert repository.save_state.call_count >= 2

    async def test_cleanup_cancels_the_loop(self, setup_persistence_service):
        service, *_ = setup_persistence_service

        service.start_periodic_checkpoints(interval_seconds=10)
        task = service._periodic_task
        service.cleanup()
        await asyncio.sleep(0)

        assert task.cancelled() or task.done()

    async def test_starting_twice_keeps_one_loop(self, setup_persistence_service):
        service, *_ = setup_persistence_service

        service.start_periodic_checkpoints(interval_seconds=10)
        task = service._periodic_task
        service.start_periodic_checkpoints(interval_seconds=10)

        assert service._periodic_task is task
        service.cleanup()
