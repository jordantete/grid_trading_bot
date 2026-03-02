from unittest.mock import MagicMock, patch

import pytest

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService


@pytest.fixture
def setup_persistence_service():
    repository = MagicMock()
    event_bus = EventBus()
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

    service = StatePersistenceService(
        repository=repository,
        event_bus=event_bus,
        order_book=order_book,
        grid_manager=grid_manager,
        balance_tracker=balance_tracker,
        config_manager=config_manager,
        trading_pair="BTC/USDT",
        strategy_type="simple_grid",
    )
    return service, repository, event_bus, order_book, grid_manager, balance_tracker


class TestEventSubscription:
    def test_subscribes_to_events(self, setup_persistence_service):
        service, _, event_bus, *_ = setup_persistence_service

        assert service._on_order_filled in event_bus.subscribers[Events.ORDER_FILLED]
        assert service._on_order_cancelled in event_bus.subscribers[Events.ORDER_CANCELLED]
        assert service._on_initial_purchase_done in event_bus.subscribers[Events.INITIAL_PURCHASE_DONE]
        assert service._on_grid_orders_initialized in event_bus.subscribers[Events.GRID_ORDERS_INITIALIZED]

    def test_cleanup_unsubscribes(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service

        service.cleanup()

        for event_type in [
            Events.ORDER_FILLED,
            Events.ORDER_CANCELLED,
            Events.INITIAL_PURCHASE_DONE,
            Events.GRID_ORDERS_INITIALIZED,
        ]:
            callbacks = event_bus.subscribers.get(event_type, [])
            assert service._on_order_filled not in callbacks
            assert service._on_order_cancelled not in callbacks
            assert service._on_initial_purchase_done not in callbacks
            assert service._on_grid_orders_initialized not in callbacks

        repository.close.assert_called_once()


class TestCheckpointOnEvents:
    async def test_checkpoint_on_order_filled(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service

        mock_order = MagicMock()
        await event_bus.publish(Events.ORDER_FILLED, mock_order)

        repository.save_bot_state.assert_called_once()
        repository.save_balance_state.assert_called_once()
        repository.save_orders.assert_called_once()
        repository.save_grid_levels.assert_called_once()

    async def test_checkpoint_on_order_cancelled(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service

        mock_order = MagicMock()
        await event_bus.publish(Events.ORDER_CANCELLED, mock_order)

        repository.save_bot_state.assert_called_once()
        repository.save_balance_state.assert_called_once()
        repository.save_orders.assert_called_once()
        repository.save_grid_levels.assert_called_once()


class TestFlagEvents:
    async def test_initial_purchase_done_sets_flag(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service

        await event_bus.publish(Events.INITIAL_PURCHASE_DONE, {})

        repository.save_bot_state.assert_called_once()
        saved_state = repository.save_bot_state.call_args[0][0]
        assert saved_state["initial_purchase_done"] is True

    async def test_grid_orders_initialized_sets_flag(self, setup_persistence_service):
        service, repository, event_bus, *_ = setup_persistence_service

        await event_bus.publish(Events.GRID_ORDERS_INITIALIZED, {})

        repository.save_bot_state.assert_called_once()
        saved_state = repository.save_bot_state.call_args[0][0]
        assert saved_state["grid_orders_initialized"] is True


class TestWriteCheckpoint:
    def test_checkpoint_writes_all_state(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        service._write_checkpoint()

        repository.save_bot_state.assert_called_once()
        saved_state = repository.save_bot_state.call_args[0][0]
        assert "config_hash" in saved_state
        assert saved_state["trading_pair"] == "BTC/USDT"
        assert saved_state["strategy_type"] == "simple_grid"
        assert saved_state["initial_purchase_done"] is False
        assert saved_state["grid_orders_initialized"] is False

        repository.save_balance_state.assert_called_once()
        repository.save_orders.assert_called_once_with([])
        repository.save_grid_levels.assert_called_once_with([])

    def test_set_flags(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        service.set_flags(initial_purchase_done=True, grid_orders_initialized=True)
        service._write_checkpoint()

        saved_state = repository.save_bot_state.call_args[0][0]
        assert saved_state["initial_purchase_done"] is True
        assert saved_state["grid_orders_initialized"] is True


class TestCheckpointErrorHandling:
    async def test_checkpoint_error_logged(self, setup_persistence_service):
        service, repository, *_ = setup_persistence_service

        repository.save_bot_state.side_effect = RuntimeError("DB write failed")

        with patch.object(service.logger, "error") as mock_error:
            await service._checkpoint()

            mock_error.assert_called_once()
            assert "Failed to write checkpoint" in mock_error.call_args[0][0]
