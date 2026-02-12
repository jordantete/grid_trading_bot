import json
from pathlib import Path

import pytest

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.config_validator import ConfigValidator
from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.bot_management.grid_trading_bot import GridTradingBot
from grid_trading_bot.core.bot_management.notification.notification_handler import NotificationHandler

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CSV_DATA_FILE = str(PROJECT_ROOT / "data" / "SOL_USDT" / "2024" / "1m.csv")

pytestmark = pytest.mark.skipif(
    not Path(CSV_DATA_FILE).exists(),
    reason=f"Integration test data not available: {CSV_DATA_FILE}",
)

# Short period for fast tests (~2880 candles at 1m)
# CSV data ranges from 2024-01-01 to 2024-10-21, SOL/USDT prices ~150-173 in Aug 2024
TEST_START_DATE = "2024-08-01T00:00:00Z"
TEST_END_DATE = "2024-08-03T00:00:00Z"


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Update integration test reference snapshots instead of comparing.",
    )


@pytest.fixture
def update_snapshots(request):
    return request.config.getoption("--update-snapshots")


@pytest.fixture
def snapshot_dir():
    return Path(__file__).resolve().parent / "snapshots"


def _build_config_dict(strategy_type: str, spacing: str) -> dict:
    return {
        "exchange": {
            "name": "binance",
            "trading_fee": 0.001,
            "trading_mode": "backtest",
        },
        "pair": {
            "base_currency": "SOL",
            "quote_currency": "USDT",
        },
        "trading_settings": {
            "timeframe": "1m",
            "period": {
                "start_date": TEST_START_DATE,
                "end_date": TEST_END_DATE,
            },
            "initial_balance": 150,
            "historical_data_file": CSV_DATA_FILE,
        },
        "grid_strategy": {
            "type": strategy_type,
            "spacing": spacing,
            "num_grids": 8,
            "range": {
                "top": 170,
                "bottom": 155,
            },
        },
        "risk_management": {
            "take_profit": {
                "enabled": False,
                "threshold": 200,
            },
            "stop_loss": {
                "enabled": False,
                "threshold": 100,
            },
        },
        "logging": {
            "log_level": "WARNING",
            "log_to_file": False,
        },
    }


@pytest.fixture
def make_config_file(tmp_path):
    """Factory fixture that writes a config JSON to tmp_path and returns its path."""

    def _make(strategy_type: str, spacing: str) -> str:
        config_dict = _build_config_dict(strategy_type, spacing)
        config_path = tmp_path / f"config_{strategy_type}_{spacing}.json"
        config_path.write_text(json.dumps(config_dict, indent=2))
        return str(config_path)

    return _make


@pytest.fixture
async def run_backtest_bot(make_config_file):
    """Factory fixture that runs a full backtest and returns (bot, result) tuple.

    The bot instance is returned so tests can inspect internal state
    (balance_tracker, grid_manager, order_book, etc.).
    """
    bots_and_event_buses = []

    async def _run(strategy_type: str, spacing: str):
        config_path = make_config_file(strategy_type, spacing)
        config_manager = ConfigManager(config_path, ConfigValidator())
        event_bus = EventBus()
        notification_handler = NotificationHandler(event_bus, [], TradingMode.BACKTEST)

        bot = GridTradingBot(
            config_path=config_path,
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=event_bus,
            save_performance_results_path=None,
            no_plot=True,
        )

        result = await bot.run()
        bots_and_event_buses.append((bot, event_bus))
        return bot, result

    yield _run

    for _, event_bus in bots_and_event_buses:
        await event_bus.shutdown()
