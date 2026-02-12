from unittest.mock import AsyncMock, Mock

import pytest

from core.bot_management.event_bus import EventBus, Events


class TestEventBus:
    @pytest.fixture
    def event_bus(self):
        return EventBus()

    def test_subscribe(self, event_bus):
        callback = Mock()
        event_bus.subscribe(Events.ORDER_FILLED, callback)
        assert Events.ORDER_FILLED in event_bus.subscribers
        assert callback in event_bus.subscribers[Events.ORDER_FILLED]

    @pytest.mark.asyncio
    async def test_publish_async_single_callback(self, event_bus):
        async_callback = AsyncMock()
        event_bus.subscribe(Events.ORDER_FILLED, async_callback)
        await event_bus.publish(Events.ORDER_FILLED, {"data": "test"})
        async_callback.assert_awaited_once_with({"data": "test"})

    @pytest.mark.asyncio
    async def test_publish_async_multiple_callbacks(self, event_bus):
        async_callback_1 = AsyncMock()
        async_callback_2 = AsyncMock()
        event_bus.subscribe(Events.ORDER_FILLED, async_callback_1)
        event_bus.subscribe(Events.ORDER_FILLED, async_callback_2)
        await event_bus.publish(Events.ORDER_FILLED, {"data": "test"})
        async_callback_1.assert_awaited_once_with({"data": "test"})
        async_callback_2.assert_awaited_once_with({"data": "test"})

    @pytest.mark.asyncio
    async def test_publish_async_with_exception(self, event_bus, caplog):
        failing_callback = AsyncMock(side_effect=Exception("Test Error"))
        event_bus.subscribe(Events.ORDER_FILLED, failing_callback)

        await event_bus.publish(Events.ORDER_FILLED, {"data": "test"})

        assert "Exception in async event callback" in caplog.text
        assert "Test Error" in caplog.text

    def test_publish_sync(self, event_bus):
        sync_callback = Mock()
        event_bus.subscribe(Events.ORDER_FILLED, sync_callback)
        event_bus.publish_sync(Events.ORDER_FILLED, {"data": "test"})
        sync_callback.assert_called_once_with({"data": "test"})

    @pytest.mark.asyncio
    async def test_safe_invoke_async(self, event_bus, caplog):
        async_callback = AsyncMock()

        await event_bus._safe_invoke_async(async_callback, {"data": "test"})

        async_callback.assert_awaited_once_with({"data": "test"})

    @pytest.mark.asyncio
    async def test_safe_invoke_async_with_exception(self, event_bus, caplog):
        failing_callback = AsyncMock(side_effect=Exception("Async Error"))

        with pytest.raises(Exception, match="Async Error"):
            await event_bus._safe_invoke_async(failing_callback, {"data": "test"})

    def test_safe_invoke_sync(self, event_bus, caplog):
        sync_callback = Mock()
        event_bus._safe_invoke_sync(sync_callback, {"data": "test"})
        sync_callback.assert_called_once_with({"data": "test"})

    def test_safe_invoke_sync_with_exception(self, event_bus, caplog):
        failing_callback = Mock(side_effect=Exception("Sync Error"))
        event_bus._safe_invoke_sync(failing_callback, {"data": "test"})
        assert "Error in sync subscriber callback" in caplog.text
