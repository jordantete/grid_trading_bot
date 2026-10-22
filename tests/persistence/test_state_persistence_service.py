import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from grid_trading_bot.config.schema import load_config
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.bot_management.stop_request import StopKind
from grid_trading_bot.core.order_handling.order import OrderSide, OrderType
from grid_trading_bot.core.order_handling.placement_journal import PendingPlacement, PlacementJournal, PlacementPurpose
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.persistence.serializers import pending_placement_to_dict
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_repository_interface import StateSnapshot


@pytest.fixture
def setup_persistence_service(valid_config):
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
    config_manager.config = load_config(valid_config)

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


class TestCleanup:
    def test_the_store_subscribes_to_nothing(self, setup_persistence_service):
        _, _, event_bus, *_ = setup_persistence_service

        assert all(not callbacks for callbacks in event_bus.subscribers.values())

    async def test_cleanup_closes_the_repository(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        await service.cleanup()

        repository.close.assert_called_once()

    async def test_cleanup_cancels_a_halt_requested_by_the_final_checkpoint(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service
        requests = []
        event_bus.subscribe(Events.STOP_BOT, requests.append)
        repository.save_state.side_effect = OSError("full")
        for _ in range(3):
            await service.save(ArmingFlags())
        halt_task = service._halt_task

        await service.cleanup()
        await asyncio.wait({halt_task}, timeout=1)

        assert halt_task.cancelled()
        assert requests == []


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

        await service.wait_for_alerts()

        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.CHECKPOINT_FAILED, error_details="disk I/O error"
        )

    async def test_a_success_after_failures_alerts_once_and_counts_again(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        repository.save_state.side_effect = [OSError("full"), OSError("full"), None, OSError("full")]

        for _ in range(4):
            await service.save(ArmingFlags())

        await service.wait_for_alerts()

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

    async def test_a_failing_alert_never_escapes_a_save(self, setup_persistence_service, caplog):
        service, repository, event_bus, *_, notification_handler = setup_persistence_service
        requests = []
        event_bus.subscribe(Events.STOP_BOT, requests.append)
        notification_handler.async_send_notification.side_effect = ValueError("bad template")
        repository.save_state.side_effect = [OSError("full"), None, OSError("full"), OSError("full"), OSError("full")]

        for _ in range(5):
            await service.save(ArmingFlags())
        await service._halt_task

        await service.wait_for_alerts()

        assert [request.kind for request in requests] == [StopKind.PERSISTENCE]
        alert_errors = [r for r in caplog.records if r.levelname == "ERROR" and r.exc_info and "alert" in r.message]
        assert len(alert_errors) == 3

    async def test_a_snapshot_that_cannot_be_built_counts_as_a_failure(self, setup_persistence_service):
        service, repository, _, order_book, *_, notification_handler = setup_persistence_service
        order_book.get_buy_orders_with_grid.side_effect = RuntimeError("book broken")

        await service.save(ArmingFlags())

        await service.wait_for_alerts()

        repository.save_state.assert_not_called()
        notification_handler.async_send_notification.assert_awaited_once()


class TestAlertsOutsideTheSave:
    async def test_a_save_does_not_wait_for_its_alert(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        released = asyncio.Event()

        async def slow(*_args, **_kwargs):
            await released.wait()

        notification_handler.async_send_notification.side_effect = slow
        repository.save_state.side_effect = OSError("full")

        await asyncio.wait_for(service.save(ArmingFlags()), timeout=1)

        assert len(service._alert_tasks) == 1
        released.set()
        await service.wait_for_alerts()
        notification_handler.async_send_notification.assert_awaited_once()

    async def test_alerts_go_out_in_the_order_they_were_raised(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        sent = []

        async def record(notification_type, **_details):
            if notification_type is NotificationType.CHECKPOINT_FAILED:
                await asyncio.sleep(0.02)
            sent.append(notification_type)

        notification_handler.async_send_notification.side_effect = record
        repository.save_state.side_effect = [OSError("full"), None]

        await service.save(ArmingFlags())
        await service.save(ArmingFlags())
        await service.wait_for_alerts()

        assert sent == [NotificationType.CHECKPOINT_FAILED, NotificationType.CHECKPOINT_RESTORED]

    async def test_cleanup_waits_for_an_alert_in_flight(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        sent = []

        async def slow(notification_type, **_details):
            await asyncio.sleep(0.02)
            sent.append(notification_type)

        notification_handler.async_send_notification.side_effect = slow
        repository.save_state.side_effect = OSError("full")
        await service.save(ArmingFlags())

        await service.cleanup()

        assert sent == [NotificationType.CHECKPOINT_FAILED]
        repository.close.assert_called_once()

    async def test_cleanup_gives_up_on_an_alert_that_hangs(self, setup_persistence_service, monkeypatch):
        service, repository, *_, notification_handler = setup_persistence_service
        monkeypatch.setattr(StatePersistenceService, "ALERT_DRAIN_TIMEOUT", 0.05)

        async def hang(*_args, **_kwargs):
            await asyncio.Event().wait()

        notification_handler.async_send_notification.side_effect = hang
        repository.save_state.side_effect = OSError("full")
        await service.save(ArmingFlags())

        await asyncio.wait_for(service.cleanup(), timeout=1)

        repository.close.assert_called_once()
        assert service._alert_tasks == set()


class TestPeriodicCheckpoints:
    async def test_the_loop_writes_on_its_interval(self, setup_persistence_service):
        service, *_ = setup_persistence_service
        checkpoint = AsyncMock()

        service.start_periodic_checkpoints(interval_seconds=0.01, checkpoint=checkpoint)
        await asyncio.sleep(0.05)
        await service.cleanup()

        assert checkpoint.await_count >= 2

    async def test_cleanup_cancels_the_loop(self, setup_persistence_service):
        service, *_ = setup_persistence_service
        checkpoint = AsyncMock()

        service.start_periodic_checkpoints(interval_seconds=10, checkpoint=checkpoint)
        task = service._periodic_task
        await service.cleanup()
        await asyncio.sleep(0)

        assert task.cancelled() or task.done()

    async def test_starting_twice_keeps_one_loop(self, setup_persistence_service):
        service, *_ = setup_persistence_service
        checkpoint = AsyncMock()

        service.start_periodic_checkpoints(interval_seconds=10, checkpoint=checkpoint)
        task = service._periodic_task
        service.start_periodic_checkpoints(interval_seconds=10, checkpoint=checkpoint)

        assert service._periodic_task is task
        await service.cleanup()

    async def test_an_exception_escaping_the_checkpoint_does_not_end_the_loop(self, setup_persistence_service):
        service, *_ = setup_persistence_service
        calls = []

        async def checkpoint():
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("lock broken")

        service.start_periodic_checkpoints(interval_seconds=0.01, checkpoint=checkpoint)
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.005)

        assert len(calls) >= 3
        assert not service._periodic_task.done()
        assert service.checkpoint_loop_state.consecutive_failures == 0
        await service.cleanup()


def _entry() -> PendingPlacement:
    return PendingPlacement(
        client_order_id="g3f2a9c1d0123456789abcdef",
        order_type=OrderType.LIMIT,
        side=OrderSide.BUY,
        quantity=0.5,
        price=95.0,
        purpose=PlacementPurpose.PAIRED,
        level_price=95.0,
        source_level_price=105.0,
        sent=False,
        created_at=1700000000000,
    )


class TestPendingPlacements:
    def test_the_snapshot_carries_the_journal_entries(self, setup_persistence_service):
        service, *_ = setup_persistence_service
        journal = PlacementJournal()
        entry = _entry()
        journal.add(entry)
        service.journal = journal

        assert service.snapshot(ArmingFlags()).pending_placements == [pending_placement_to_dict(entry)]

    def test_a_snapshot_without_a_journal_carries_no_entry(self, setup_persistence_service):
        service, *_ = setup_persistence_service

        assert service.snapshot(ArmingFlags()).pending_placements == []

    async def test_an_entry_is_written_as_one_row(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service
        entry = _entry()

        assert await service.save_pending(entry) is True

        repository.save_pending.assert_called_once_with(pending_placement_to_dict(entry))

    async def test_a_failed_entry_write_returns_false_and_alerts(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        repository.save_pending.side_effect = OSError("disk I/O error")

        assert await service.save_pending(_entry()) is False

        await service.wait_for_alerts()
        notification_handler.async_send_notification.assert_awaited_once_with(
            NotificationType.CHECKPOINT_FAILED, error_details="disk I/O error"
        )

    async def test_entry_writes_and_checkpoints_share_one_failure_count(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service
        requests = []
        event_bus.subscribe(Events.STOP_BOT, requests.append)
        repository.save_pending.side_effect = OSError("full")
        repository.save_state.side_effect = OSError("full")

        await service.save_pending(_entry())
        await service.save_pending(_entry())
        await service.save(ArmingFlags())
        await service._halt_task

        assert [request.kind for request in requests] == [StopKind.PERSISTENCE]

    async def test_an_entry_written_after_failures_restores_the_count(self, setup_persistence_service):
        service, repository, *_, notification_handler = setup_persistence_service
        repository.save_state.side_effect = OSError("full")
        await service.save(ArmingFlags())

        assert await service.save_pending(_entry()) is True

        await service.wait_for_alerts()
        sent = notification_handler.async_send_notification.await_args_list
        assert [call.args[0] for call in sent] == [
            NotificationType.CHECKPOINT_FAILED,
            NotificationType.CHECKPOINT_RESTORED,
        ]
        assert service._consecutive_failures == 0
