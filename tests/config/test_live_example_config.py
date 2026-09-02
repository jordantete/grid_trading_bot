from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.config.config_validator import ConfigValidator


class TestLiveExampleConfig:
    def test_example_validates_and_is_paper_trading(self):
        cm = ConfigManager("config/config.live.example.json", ConfigValidator())
        assert cm.get_trading_mode().value == "paper_trading"
        assert cm.is_persistence_enabled() is True
        assert cm.get_checkpoint_interval_seconds() == 60.0
        assert cm.is_trailing_stop_loss_enabled() is True
        assert cm.is_dynamic_spacing_enabled() is True

    def test_example_declares_maker_taker_fees_and_post_only(self):
        """
        The accessors fall back to `trading_fee` / False, so asserting on them would pass
        with the keys absent. Read the raw config to prove the example actually ships them
        — that is what makes the options discoverable to someone copying this file.
        """
        cm = ConfigManager("config/config.live.example.json", ConfigValidator())
        exchange = cm.get_exchange()
        # Binance spot tier 0 charges the same rate on both sides; the split is still
        # declared so the example shows where a two-tier venue would set them.
        assert exchange["maker_fee"] == 0.001
        assert exchange["taker_fee"] == 0.001
        # Grid limit orders are meant to rest on the book, and a rejection is now handled as
        # a skipped level rather than a failure, so the template ships with it enabled.
        assert exchange["post_only"] is True
