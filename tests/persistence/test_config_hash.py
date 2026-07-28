import hashlib
import json
from unittest.mock import MagicMock

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.persistence.serializers import compute_config_hash


@pytest.fixture
def config_manager_factory():
    """Factory fixture to create mock ConfigManager instances with customizable fields."""

    def _factory(
        grid_settings=None,
        pair=None,
        dynamic_spacing=None,
        exchange_name=None,
        trading_mode=None,
        risk_management=None,
        timeframe=None,
    ):
        cm = MagicMock()
        cm.get_grid_settings.return_value = grid_settings or {
            "type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": 10,
            "range": [40000, 50000],
        }
        cm.get_pair.return_value = pair or {"base": "BTC", "quote": "USDT"}
        cm.get_dynamic_spacing.return_value = dynamic_spacing or {}
        cm.get_exchange_name.return_value = exchange_name or "binance"
        cm.get_trading_mode.return_value = trading_mode or TradingMode.BACKTEST
        cm.get_risk_management.return_value = risk_management or {}
        cm.get_timeframe.return_value = timeframe or "1h"
        return cm

    return _factory


class TestComputeConfigHash:
    """Tests for compute_config_hash determinism, sensitivity, and stability."""

    def _make_config_manager(self, grid_settings=None, pair=None):
        """Helper to create a mock ConfigManager with the given grid settings and pair."""
        cm = MagicMock()
        cm.get_grid_settings.return_value = grid_settings or {
            "type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": 10,
            "range": [40000, 50000],
        }
        cm.get_pair.return_value = pair or {"base": "BTC", "quote": "USDT"}
        cm.get_dynamic_spacing.return_value = {}
        cm.get_exchange_name.return_value = "binance"
        cm.get_trading_mode.return_value = TradingMode.BACKTEST
        cm.get_risk_management.return_value = {}
        cm.get_timeframe.return_value = "1h"
        return cm

    def test_hash_is_deterministic(self):
        """The same config always produces the same hash."""
        cm1 = self._make_config_manager()
        cm2 = self._make_config_manager()

        hash1 = compute_config_hash(cm1)
        hash2 = compute_config_hash(cm2)

        assert hash1 == hash2

    def test_hash_changes_when_strategy_type_changes(self):
        """Changing the grid strategy type produces a different hash."""
        cm_simple = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "arithmetic", "num_grids": 10, "range": [40000, 50000]}
        )
        cm_hedged = self._make_config_manager(
            grid_settings={"type": "hedged_grid", "spacing": "arithmetic", "num_grids": 10, "range": [40000, 50000]}
        )

        assert compute_config_hash(cm_simple) != compute_config_hash(cm_hedged)

    def test_hash_changes_when_num_grids_changes(self):
        """Changing num_grids produces a different hash."""
        cm_10 = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "arithmetic", "num_grids": 10, "range": [40000, 50000]}
        )
        cm_20 = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "arithmetic", "num_grids": 20, "range": [40000, 50000]}
        )

        assert compute_config_hash(cm_10) != compute_config_hash(cm_20)

    def test_hash_changes_when_range_changes(self):
        """Changing the price range produces a different hash."""
        cm_narrow = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "arithmetic", "num_grids": 10, "range": [40000, 50000]}
        )
        cm_wide = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "arithmetic", "num_grids": 10, "range": [30000, 60000]}
        )

        assert compute_config_hash(cm_narrow) != compute_config_hash(cm_wide)

    def test_hash_changes_when_pair_changes(self):
        """Changing the trading pair produces a different hash."""
        cm_btc = self._make_config_manager(pair={"base": "BTC", "quote": "USDT"})
        cm_eth = self._make_config_manager(pair={"base": "ETH", "quote": "USDT"})

        assert compute_config_hash(cm_btc) != compute_config_hash(cm_eth)

    def test_hash_is_stable_known_output(self):
        """A known input always maps to a known hash value (regression guard)."""
        cm = self._make_config_manager(
            grid_settings={
                "type": "simple_grid",
                "spacing": "arithmetic",
                "num_grids": 10,
                "range": [40000, 50000],
            },
            pair={"base": "BTC", "quote": "USDT"},
        )

        # Reproduce the expected hash independently
        hash_input = {
            "strategy_type": "simple_grid",
            "spacing": "arithmetic",
            "num_grids": 10,
            "range": [40000, 50000],
            "buy_ratio": 1.0,
            "sell_ratio": 1.0,
            "pair": {"base": "BTC", "quote": "USDT"},
            "dynamic_spacing": None,
            "exchange_name": "binance",
            "trading_mode": "backtest",
            "risk_management": None,
            "timeframe": "1h",
        }
        canonical = json.dumps(hash_input, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(canonical.encode()).hexdigest()

        assert compute_config_hash(cm) == expected_hash

    def test_hash_ignores_irrelevant_config_fields(self):
        """Fields not included in the hash input (e.g., logging, risk_management) do not affect the hash."""
        grid_settings_base = {
            "type": "simple_grid",
            "spacing": "geometric",
            "num_grids": 15,
            "range": [35000, 45000],
        }
        pair = {"base": "ETH", "quote": "USDT"}

        # Config with only the fields used by the hash
        cm_minimal = self._make_config_manager(grid_settings=dict(grid_settings_base), pair=dict(pair))

        # Config with extra fields that should be ignored
        grid_settings_extra = dict(grid_settings_base)
        grid_settings_extra["take_profit"] = 0.05
        grid_settings_extra["stop_loss"] = 0.03
        grid_settings_extra["logging_level"] = "DEBUG"
        cm_extra = self._make_config_manager(grid_settings=grid_settings_extra, pair=dict(pair))

        assert compute_config_hash(cm_minimal) == compute_config_hash(cm_extra)

    def test_hash_changes_when_spacing_changes(self):
        """Changing spacing from arithmetic to geometric produces a different hash."""
        cm_arith = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "arithmetic", "num_grids": 10, "range": [40000, 50000]}
        )
        cm_geo = self._make_config_manager(
            grid_settings={"type": "simple_grid", "spacing": "geometric", "num_grids": 10, "range": [40000, 50000]}
        )

        assert compute_config_hash(cm_arith) != compute_config_hash(cm_geo)

    def test_hash_changes_when_ratios_change(self):
        """Changing buy_ratio or sell_ratio produces a different hash."""
        base = {"type": "simple_grid", "spacing": "arithmetic", "num_grids": 10, "range": [40000, 50000]}
        cm_default = self._make_config_manager(grid_settings=dict(base))
        cm_with_ratio = self._make_config_manager(grid_settings={**base, "sell_ratio": 0.5})

        assert compute_config_hash(cm_default) != compute_config_hash(cm_with_ratio)

    def test_hash_is_valid_sha256_hex_digest(self):
        """The returned hash is a 64-character lowercase hex string (SHA-256)."""
        cm = self._make_config_manager()
        result = compute_config_hash(cm)

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_hash_changes_when_dynamic_spacing_changes(self, config_manager_factory):
        """Changing dynamic_spacing produces a different hash."""
        base = config_manager_factory()
        changed = config_manager_factory(dynamic_spacing={"enabled": True, "atr_period": 14})
        assert compute_config_hash(base) != compute_config_hash(changed)

    def test_hash_changes_when_exchange_or_mode_changes(self, config_manager_factory):
        """Changing exchange_name produces a different hash."""
        assert compute_config_hash(config_manager_factory(exchange_name="binance")) != compute_config_hash(
            config_manager_factory(exchange_name="kraken")
        )

    def test_hash_changes_when_risk_management_changes(self, config_manager_factory):
        """Changing risk_management produces a different hash."""
        base = config_manager_factory()
        changed = config_manager_factory(risk_management={"take_profit": {"enabled": True, "threshold": 3700}})
        assert compute_config_hash(base) != compute_config_hash(changed)

    def test_hash_changes_when_timeframe_changes(self, config_manager_factory):
        """Changing timeframe produces a different hash."""
        assert compute_config_hash(config_manager_factory(timeframe="1m")) != compute_config_hash(
            config_manager_factory(timeframe="1h")
        )

    def test_hash_stable_for_identical_config(self, config_manager_factory):
        """The same factory configuration always produces the same hash."""
        assert compute_config_hash(config_manager_factory()) == compute_config_hash(config_manager_factory())
