import asyncio
import logging
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from tests.fakes.config import bot_config_from
from tests.fixtures.strategies import use_trailing_stop

from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.stop_request import StopKind
from grid_trading_bot.core.order_handling.balance_tracker import AccountSnapshot
from grid_trading_bot.core.services.exceptions import DataFetchError
from grid_trading_bot.strategies.live_grid_strategy import LiveGridStrategy


class TestLiveGridStrategy:
    @pytest.mark.asyncio
    async def test_run_live_trading(self, setup_strategy):
        create_strategy, _, exchange_service, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        exchange_service.listen_to_ticker_updates = AsyncMock()

        await strategy.run()

        exchange_service.listen_to_ticker_updates.assert_called_once()

    def test_plot_results_not_available_in_live_mode(self, setup_strategy, caplog):
        create_strategy, _, _, _, _, _, _, plotter, _ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)

        with caplog.at_level(logging.INFO):
            strategy.plot_results()

        assert "Plotting is not available for live/paper trading mode." in [record.message for record in caplog.records]
        plotter.plot_results.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_live_run_loads_no_history(self, setup_strategy):
        create_strategy, _, exchange_service, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        exchange_service.listen_to_ticker_updates = AsyncMock()

        await strategy.run()

        exchange_service.fetch_ohlcv.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode_label", ["live", "paper"])
    async def test_the_session_log_names_its_mode_label(self, mode_label, setup_strategy, caplog):
        create_strategy, _, exchange_service, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy, mode_label)
        exchange_service.listen_to_ticker_updates = AsyncMock()

        with caplog.at_level(logging.INFO, logger="LiveGridStrategy"):
            await strategy.run()

        assert f"Starting {mode_label} trading" in [record.getMessage() for record in caplog.records]

    def test_generate_performance_report_live_mode(self, setup_strategy):
        create_strategy, _, _, _, _, balance_tracker, trading_performance_analyzer, _, _ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)

        balance_tracker.get_adjusted_fiat_balance.return_value = 5000
        balance_tracker.get_adjusted_crypto_balance.return_value = 1
        balance_tracker.total_fees = 10

        strategy.live_trading_metrics = [
            (pd.Timestamp("2024-01-01"), 10000, 100),
            (pd.Timestamp("2024-01-02"), 11000, 110),
        ]
        balance_tracker.snapshot.return_value = AccountSnapshot(
            price=11000.0, value=16000.0, peak=16000.0, drawdown_pct=0.0, realized_pnl=42.0, unrealized_pnl=-3.0
        )

        strategy.generate_performance_report()

        balance_tracker.snapshot.assert_called_once_with(110)
        trading_performance_analyzer.generate_performance_summary.assert_called_once()
        assert trading_performance_analyzer.generate_performance_summary.call_args.args[-2:] == (42.0, -3.0)

    def test_generate_performance_report_live_mode_no_metrics(self, setup_strategy):
        create_strategy, _, _, _, _, _, trading_performance_analyzer, _, _ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)

        metrics, result, formatted_orders = strategy.generate_performance_report()

        assert metrics is None
        assert result == {}
        assert formatted_orders == []
        trading_performance_analyzer.generate_performance_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_live_trading_error_handling(self, setup_strategy):
        create_strategy, _, exchange_service, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=Exception("Connection error"))

        await strategy.run()

        exchange_service.listen_to_ticker_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_dead_feed_publishes_a_feed_lost_stop(self, setup_strategy):
        create_strategy, _, exchange_service, _, _, _, _, _, event_bus = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=DataFetchError("feed died"))

        await strategy._run_live_or_paper_trading()

        request = event_bus.publish.await_args.args[1]
        assert event_bus.publish.await_args.args[0] == Events.STOP_BOT
        assert request.kind is StopKind.FEED_LOST
        assert "feed died" in request.reason
        assert strategy._running is False

    @pytest.mark.asyncio
    async def test_an_unexpected_loop_error_publishes_a_crash_stop(self, setup_strategy):
        create_strategy, _, exchange_service, _, _, _, _, _, event_bus = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=RuntimeError("bug"))

        await strategy._run_live_or_paper_trading()

        assert event_bus.publish.await_args.args[1].kind is StopKind.CRASH

    @pytest.mark.asyncio
    async def test_run_live_trading_stop_condition(self, setup_strategy):
        create_strategy, _, exchange_service, _, _, _, _, _, _ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)

        async def stop_strategy(*args, **kwargs):
            strategy._running = False

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=stop_strategy)

        await strategy.run()

        assert not strategy._running
        exchange_service.listen_to_ticker_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ticker_update_error_handling(self, setup_strategy):
        create_strategy, _, exchange_service, grid_manager, _order_manager, balance_tracker, _, _, _ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)

        grid_manager.get_trigger_price.return_value = 15000
        balance_tracker.get_total_balance_value.side_effect = Exception("Balance calculation error")

        async def simulate_ticker_update():
            callback = exchange_service.listen_to_ticker_updates.call_args[0][1]
            await callback(15100)

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=simulate_ticker_update)

        await strategy.run()

        exchange_service.listen_to_ticker_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_volatility_regrid_skipped_on_non_candle_tick(self, setup_strategy):
        """A ticker tick that does not close a candle must not evaluate the volatility regrid."""
        create_strategy, _config, exchange_service, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        strategy._initialize_grid_orders_once = AsyncMock(return_value=True)
        strategy._maybe_refresh_live_atr = AsyncMock(return_value=False)
        strategy._maybe_regrid_on_volatility = AsyncMock()
        strategy._handle_trailing_stop = AsyncMock(return_value=False)
        strategy._handle_take_profit_stop_loss = AsyncMock(return_value=False)

        async def simulate_ticker_update(*args, **kwargs):
            callback = exchange_service.listen_to_ticker_updates.call_args[0][1]
            await callback(15100)

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=simulate_ticker_update)

        await strategy.run()

        strategy._maybe_refresh_live_atr.assert_awaited_once()
        strategy._maybe_regrid_on_volatility.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_volatility_regrid_evaluated_on_candle_close_tick(self, setup_strategy):
        """A ticker tick that closes a candle (refresh returned True) evaluates the volatility regrid."""
        create_strategy, _config, exchange_service, *_ = setup_strategy
        strategy = create_strategy(LiveGridStrategy)
        strategy._initialize_grid_orders_once = AsyncMock(return_value=True)
        strategy._maybe_refresh_live_atr = AsyncMock(return_value=True)
        strategy._maybe_regrid_on_volatility = AsyncMock()
        strategy._handle_trailing_stop = AsyncMock(return_value=False)
        strategy._handle_take_profit_stop_loss = AsyncMock(return_value=False)

        async def simulate_ticker_update(*args, **kwargs):
            callback = exchange_service.listen_to_ticker_updates.call_args[0][1]
            await callback(15100)

        exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=simulate_ticker_update)

        await strategy.run()

        strategy._maybe_regrid_on_volatility.assert_awaited_once()


class TestLiveAtrRefresh:
    async def test_refresh_computes_atr_after_candle_close(self, live_strategy_fixture, valid_config):
        s = live_strategy_fixture
        s.config_manager.config = bot_config_from(valid_config, {"trading_settings": {"timeframe": "1m"}})
        use_trailing_stop(s, atr_period=3)
        candles = pd.DataFrame(
            [[pd.Timestamp("2026-01-01 00:00"), 100, 101, 99, 100, 1]] * 6,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        s.exchange_service.fetch_recent_ohlcv = AsyncMock(return_value=candles)
        s._next_candle_close = pd.Timestamp("2026-01-01 00:01:00")

        refreshed = await s._maybe_refresh_live_atr(now=pd.Timestamp("2026-01-01 00:01:05"))

        assert refreshed is True  # fetched a fresh candle close
        s.exchange_service.fetch_recent_ohlcv.assert_awaited_once()
        assert 3 in s._live_atr
        assert s._next_candle_close == pd.Timestamp("2026-01-01 00:02:00")

    async def test_no_fetch_before_candle_close(self, live_strategy_fixture):
        s = live_strategy_fixture
        use_trailing_stop(s)
        s.exchange_service.fetch_recent_ohlcv = AsyncMock()
        s._next_candle_close = pd.Timestamp("2026-01-01 00:01:00")

        refreshed = await s._maybe_refresh_live_atr(now=pd.Timestamp("2026-01-01 00:00:30"))

        assert refreshed is False  # boundary not reached
        s.exchange_service.fetch_recent_ohlcv.assert_not_awaited()

    async def test_first_call_arms_boundary_without_fetch(self, live_strategy_fixture, valid_config):
        s = live_strategy_fixture
        s.config_manager.config = bot_config_from(valid_config, {"trading_settings": {"timeframe": "1m"}})
        use_trailing_stop(s, atr_period=3)
        s.exchange_service.fetch_recent_ohlcv = AsyncMock()
        s._next_candle_close = None

        refreshed = await s._maybe_refresh_live_atr(now=pd.Timestamp("2026-01-01 00:00:30"))

        assert refreshed is False  # not armed yet
        s.exchange_service.fetch_recent_ohlcv.assert_not_awaited()
        assert s._next_candle_close is not None

    async def test_no_features_returns_false(self, live_strategy_fixture):
        s = live_strategy_fixture

        refreshed = await s._maybe_refresh_live_atr(now=pd.Timestamp("2026-01-01 00:01:05"))

        assert refreshed is False

    async def test_fetch_failure_keeps_last_atr(self, live_strategy_fixture, valid_config):
        s = live_strategy_fixture
        s.config_manager.config = bot_config_from(valid_config, {"trading_settings": {"timeframe": "1m"}})
        use_trailing_stop(s, atr_period=3)
        s._live_atr[3] = 2.5
        s.exchange_service.fetch_recent_ohlcv = AsyncMock(side_effect=DataFetchError("boom"))
        s._next_candle_close = pd.Timestamp("2026-01-01 00:01:00")

        refreshed = await s._maybe_refresh_live_atr(now=pd.Timestamp("2026-01-01 00:01:05"))

        assert refreshed is False  # fetch failed
        assert s._live_atr[3] == 2.5  # unchanged, bot keeps running

    def test_timeframe_to_seconds_month(self, live_strategy_fixture):
        # "1M" (month) must map, not KeyError — the config schema accepts it as a timeframe.
        assert live_strategy_fixture._timeframe_to_seconds("1M") == 2_592_000
        assert live_strategy_fixture._timeframe_to_seconds("1m") == 60  # minute still distinct


class TestTickDrain:
    @staticmethod
    def _live_with_one_tick(strategy):

        async def one_tick(_pair, on_tick, _interval):
            await on_tick(100.0)

        strategy.exchange_service.listen_to_ticker_updates = AsyncMock(side_effect=one_tick)
        return strategy

    @staticmethod
    def _block_the_tick(strategy, may_finish: asyncio.Event, events: list) -> asyncio.Event:
        started = asyncio.Event()

        async def slow_arming(*_args, **_kwargs):
            started.set()
            await may_finish.wait()
            events.append("tick finished")
            return False

        strategy._initialize_grid_orders_once = AsyncMock(side_effect=slow_arming)
        return started

    async def test_the_drain_waits_for_a_ticker_update_in_flight(self, live_strategy_fixture):
        s = self._live_with_one_tick(live_strategy_fixture)
        may_finish = asyncio.Event()
        events = []
        started = self._block_the_tick(s, may_finish, events)
        run = asyncio.create_task(s.run())
        await asyncio.wait_for(started.wait(), timeout=1)

        drain = asyncio.create_task(s.wait_for_tick_to_finish(timeout=5))
        await asyncio.sleep(0.02)
        assert not drain.done()

        may_finish.set()
        await asyncio.wait_for(drain, timeout=1)
        events.append("drained")
        await asyncio.wait_for(run, timeout=1)

        assert events == ["tick finished", "drained"]

    async def test_a_stop_published_inside_a_ticker_update_does_not_wait_for_it(self, live_strategy_fixture):
        s = self._live_with_one_tick(live_strategy_fixture)
        s.event_bus = EventBus()
        drained = []

        async def on_stop(_request):
            await s.wait_for_tick_to_finish(timeout=30)
            drained.append(True)

        s.event_bus.subscribe(Events.STOP_BOT, on_stop)
        s._initialize_grid_orders_once = AsyncMock(return_value=True)
        s._maybe_refresh_live_atr = AsyncMock(return_value=False)
        s._handle_trailing_stop = AsyncMock(return_value=False)
        s._evaluate_tp_or_sl = AsyncMock(return_value=True)

        await asyncio.wait_for(s.run(), timeout=1)

        assert drained == [True]

    async def test_the_drain_gives_up_after_its_timeout_with_a_warning(self, live_strategy_fixture, caplog):
        s = self._live_with_one_tick(live_strategy_fixture)
        may_finish = asyncio.Event()
        started = self._block_the_tick(s, may_finish, [])
        run = asyncio.create_task(s.run())
        await asyncio.wait_for(started.wait(), timeout=1)

        with caplog.at_level(logging.WARNING, logger="LiveGridStrategy"):
            await asyncio.wait_for(s.wait_for_tick_to_finish(timeout=0.05), timeout=1)

        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "ticker update" in warnings[0].getMessage()
        may_finish.set()
        await asyncio.wait_for(run, timeout=1)
