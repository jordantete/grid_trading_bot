import threading
from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events


class TestEventBus:
    @pytest.fixture
    def event_bus(self):
        return EventBus()

    def test_subscribe(self, event_bus):
        callback = Mock()
        event_bus.subscribe(Events.STOP_BOT, callback)
        assert Events.STOP_BOT in event_bus.subscribers
        assert callback in event_bus.subscribers[Events.STOP_BOT]

    @pytest.mark.asyncio
    async def test_publish_async_single_callback(self, event_bus):
        async_callback = AsyncMock()
        event_bus.subscribe(Events.STOP_BOT, async_callback)
        await event_bus.publish(Events.STOP_BOT, {"data": "test"})
        async_callback.assert_awaited_once_with({"data": "test"})

    @pytest.mark.asyncio
    async def test_publish_async_multiple_callbacks(self, event_bus):
        async_callback_1 = AsyncMock()
        async_callback_2 = AsyncMock()
        event_bus.subscribe(Events.STOP_BOT, async_callback_1)
        event_bus.subscribe(Events.STOP_BOT, async_callback_2)
        await event_bus.publish(Events.STOP_BOT, {"data": "test"})
        async_callback_1.assert_awaited_once_with({"data": "test"})
        async_callback_2.assert_awaited_once_with({"data": "test"})

    @pytest.mark.asyncio
    async def test_publish_async_with_exception(self, event_bus, caplog):
        failing_callback = AsyncMock(side_effect=Exception("Test Error"))
        event_bus.subscribe(Events.STOP_BOT, failing_callback)

        await event_bus.publish(Events.STOP_BOT, {"data": "test"})

        assert "Exception in async event callback" in caplog.text
        assert "Test Error" in caplog.text

    @pytest.mark.asyncio
    async def test_publish_runs_sync_subscribers_on_the_loop_thread(self, event_bus):
        threads = []
        event_bus.subscribe(Events.STOP_BOT, lambda _data: threads.append(threading.get_ident()))

        await event_bus.publish(Events.STOP_BOT, "x")

        assert threads == [threading.get_ident()]

    @pytest.mark.asyncio
    async def test_a_subscriber_may_unsubscribe_during_publish(self, event_bus):
        calls = []

        def first(_data):
            calls.append("first")
            event_bus.unsubscribe(Events.STOP_BOT, first)

        event_bus.subscribe(Events.STOP_BOT, first)
        event_bus.subscribe(Events.STOP_BOT, lambda _data: calls.append("second"))

        await event_bus.publish(Events.STOP_BOT, "x")

        assert calls == ["first", "second"]

    def test_publish_sync(self, event_bus):
        sync_callback = Mock()
        event_bus.subscribe(Events.STOP_BOT, sync_callback)
        event_bus.publish_sync(Events.STOP_BOT, {"data": "test"})
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
