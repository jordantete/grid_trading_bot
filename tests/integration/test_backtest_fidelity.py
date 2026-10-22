"""Backtest fidelity checks on small synthetic price paths, full bot stack, no mocks."""

import pandas as pd
import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler
from grid_trading_bot.core.bot_management.stop_request import StopKind
from grid_trading_bot.core.services.market_constraints import MarketConstraints

pytestmark = pytest.mark.integration

Candle = tuple[float, float, float, float]


def _flat(price: float, count: int) -> list[Candle]:
    return [(price, price + 0.2, price - 0.2, price) for _ in range(count)]


def _ramp(start: float, step: float, count: int) -> list[Candle]:
    rows = []
    price = start
    for _ in range(count):
        new_price = price + step
        rows.append((price, max(price, new_price) + 0.1, min(price, new_price) - 0.1, new_price))
        price = new_price
    return rows


# The audit's stop-loss path: flat at 100, down to 80, back up to 120.
DROP_THEN_RALLY = _flat(100.0, 30) + _ramp(100.0, -0.5, 40) + _ramp(80.0, 0.5, 80)
RALLY = _flat(100.0, 30) + _ramp(100.0, 0.5, 40) + _flat(120.0, 30)
TRAILING_STOP = {"enabled": True, "atr_period": 14, "atr_multiplier": 2.5, "on_trigger": "stop"}


def _write_candles(tmp_path, rows: list[Candle]) -> tuple[str, str, str]:
    index = pd.date_range("2024-01-01 00:00:00", periods=len(rows), freq="1min")
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame["volume"] = 1.0
    frame.insert(0, "timestamp", index)
    path = tmp_path / "candles.csv"
    frame.to_csv(path, index=False)
    return str(path), index[0].strftime("%Y-%m-%dT%H:%M:%SZ"), index[-1].strftime("%Y-%m-%dT%H:%M:%SZ")


def _config(tmp_path, rows: list[Candle], *, risk_management=None, execution=None, **grid_overrides) -> dict:
    csv_path, start, end = _write_candles(tmp_path, rows)
    config = {
        "exchange": {"name": "binance", "trading_fee": 0.0, "trading_mode": "backtest"},
        "pair": {"base_currency": "SOL", "quote_currency": "USDT"},
        "trading_settings": {
            "timeframe": "1m",
            "period": {"start_date": start, "end_date": end},
            "initial_balance": 1000,
            "historical_data_file": csv_path,
        },
        "grid_strategy": {
            "type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": 8,
            "range": {"top": 110.0, "bottom": 90.0},
            **grid_overrides,
        },
        "risk_management": {
            "take_profit": {"enabled": False, "threshold": 1e9},
            "stop_loss": {"enabled": False, "threshold": 0.0001},
            **(risk_management or {}),
        },
        "logging": {"log_level": "WARNING", "log_to_file": False},
    }
    if execution:
        config["execution"] = execution
    return config


async def _run(config: dict, event_bus: EventBus | None = None):
    event_bus = event_bus or EventBus()
    bot = GridTradingBot(
        config_path="<fidelity>",
        config_manager=ConfigManager.from_dict(config),
        notification_handler=NotificationHandler([], TradingMode.BACKTEST),
        event_bus=event_bus,
        no_plot=True,
    )
    try:
        result = await bot.run()
    finally:
        await event_bus.shutdown()
    return bot, result


def _roi(result) -> float:
    return float(result["performance_summary"]["ROI"].rstrip("%"))


def _assert_flat_after_liquidation(bot) -> None:
    tracker = bot.balance_tracker
    assert bot.order_book.get_open_orders() == []
    assert tracker.reserved_fiat == 0
    assert tracker.reserved_crypto == 0
    assert tracker.get_adjusted_crypto_balance() == pytest.approx(0.0, abs=1e-9)


async def test_stop_loss_liquidates_the_whole_position(tmp_path):
    config = _config(tmp_path, DROP_THEN_RALLY, risk_management={"stop_loss": {"enabled": True, "threshold": 85.0}})

    bot, result = await _run(config)

    _assert_flat_after_liquidation(bot)
    assert _roi(result) < -10.0


async def test_take_profit_liquidates_the_whole_position(tmp_path):
    config = _config(tmp_path, RALLY, risk_management={"take_profit": {"enabled": True, "threshold": 104.0}})

    bot, result = await _run(config)

    _assert_flat_after_liquidation(bot)
    assert _roi(result) > 0.0


async def test_trailing_stop_liquidates_the_whole_position(tmp_path):
    config = _config(tmp_path, DROP_THEN_RALLY, risk_management={"trailing_stop_loss": TRAILING_STOP})

    bot, _ = await _run(config)

    _assert_flat_after_liquidation(bot)


async def test_backtest_never_places_a_grid_order_below_the_configured_minimum(tmp_path):
    # Ratios below 1 shrink paired orders under the minimum, which the exchange would refuse.
    config = _config(tmp_path, DROP_THEN_RALLY, execution={"backtest_min_cost": 85.0}, buy_ratio=0.8, sell_ratio=0.8)

    bot, _ = await _run(config)

    assert bot.order_manager.market_constraints == MarketConstraints(min_amount=None, min_cost=85.0)
    grid_orders = [
        order
        for order, level in bot.order_book.get_buy_orders_with_grid() + bot.order_book.get_sell_orders_with_grid()
        if level is not None
    ]
    assert grid_orders
    assert all(order.amount * order.price >= 85.0 for order in grid_orders)


async def test_backtest_without_minimum_keys_has_no_constraints(tmp_path):
    bot, _ = await _run(_config(tmp_path, DROP_THEN_RALLY))

    assert bot.order_manager.market_constraints is None


async def test_account_is_valued_flat_after_a_stop_loss(tmp_path):
    config = _config(tmp_path, DROP_THEN_RALLY, risk_management={"stop_loss": {"enabled": True, "threshold": 85.0}})

    bot, result = await _run(config)

    data = bot.strategy.data
    account_value = data["account_value"]
    assert account_value.notna().all()
    stop_index = int((data["close"] <= 85.0).to_numpy().argmax())
    assert (account_value.iloc[stop_index:] == account_value.iloc[stop_index]).all()
    final_balance = float(result["performance_summary"]["Final Balance (Fiat)"])
    assert account_value.iloc[-1] == pytest.approx(final_balance, abs=0.01)
    assert bot.strategy.stop_reason == "stop_loss"
    assert bot.strategy.armed is True


@pytest.mark.parametrize(
    ("rows", "risk_management", "expected_reason"),
    [
        (RALLY, {"take_profit": {"enabled": True, "threshold": 104.0}}, "take_profit"),
        (DROP_THEN_RALLY, {"trailing_stop_loss": TRAILING_STOP}, "trailing_stop"),
    ],
)
async def test_account_is_valued_after_any_stop(tmp_path, rows, risk_management, expected_reason):
    bot, _ = await _run(_config(tmp_path, rows, risk_management=risk_management))

    assert bot.strategy.stop_reason == expected_reason
    assert bot.strategy.data["account_value"].notna().all()


async def test_infeasible_grid_stops_the_backtest_once(tmp_path):
    event_bus = EventBus()
    stop_events = []

    async def record_stop(request):
        stop_events.append(request)

    event_bus.subscribe(Events.STOP_BOT, record_stop)
    config = _config(tmp_path, DROP_THEN_RALLY, execution={"backtest_min_cost": 1_000_000.0})

    bot, result = await _run(config, event_bus)

    assert len(stop_events) == 1
    assert stop_events[0].kind is StopKind.INFEASIBLE
    assert bot.strategy.stop_reason == "grid_infeasible"
    assert bot.strategy.armed is False
    assert bot.strategy.data["account_value"].notna().all()
    assert _roi(result) == 0.0


async def test_orders_placed_at_a_candle_close_first_meet_the_next_candle(tmp_path):
    # Candle 0 closes at 100 inside the grid; its low (92) would cross three buy levels.
    rows = [(100.0, 100.2, 92.0, 100.0), (100.0, 100.2, 98.0, 100.0), *_flat(100.0, 5)]

    bot, _ = await _run(_config(tmp_path, rows))

    filled_grid_buys = [
        order for order, level in bot.order_book.get_buy_orders_with_grid() if level is not None and order.is_filled()
    ]
    assert [round(order.price, 2) for order in filled_grid_buys] == [98.57]


async def test_stop_loss_on_the_candle_after_arming(tmp_path):
    rows = _flat(100.0, 1) + _ramp(100.0, -5.0, 3) + _flat(85.0, 5)
    config = _config(tmp_path, rows, risk_management={"stop_loss": {"enabled": True, "threshold": 96.0}})

    bot, _ = await _run(config)

    account_value = bot.strategy.data["account_value"]
    assert bot.strategy.stop_reason == "stop_loss"
    assert account_value.notna().all()
    assert (account_value.iloc[1:] == account_value.iloc[1]).all()


async def test_metrics_of_a_window_where_the_grid_never_arms(tmp_path):
    _, result = await _run(_config(tmp_path, _flat(200.0, 30)))

    metrics = result["metrics"]
    assert metrics.armed is False
    assert metrics.avg_crypto_exposure == 0.0
    assert metrics.roi_pct == 0.0
    assert metrics.equity_coverage == 1.0
    assert metrics.stop_reason is None


async def test_metrics_after_a_stop_loss(tmp_path):
    config = _config(tmp_path, DROP_THEN_RALLY, risk_management={"stop_loss": {"enabled": True, "threshold": 85.0}})

    _, result = await _run(config)

    metrics = result["metrics"]
    assert metrics.stop_reason == "stop_loss"
    assert metrics.armed is True
    assert metrics.equity_coverage == 1.0
    assert 0.0 < metrics.avg_crypto_exposure < 1.0
    assert metrics.roi_pct == pytest.approx(_roi(result), abs=0.005)


async def test_roi_is_measured_from_the_cash_before_the_initial_purchase(tmp_path):
    config = _config(tmp_path, _flat(100.0, 30), execution={"backtest_slippage": 0.0005})
    config["exchange"]["trading_fee"] = 0.001

    _, result = await _run(config)

    metrics = result["metrics"]
    assert metrics.armed is True
    assert metrics.roi_pct < 0
    # The analyzer rounds a numpy float, which can round a tie such as -0.075 the other way from Python's round.
    assert metrics.roi_pct == pytest.approx((metrics.final_balance - 1000) / 1000 * 100, abs=0.01)
