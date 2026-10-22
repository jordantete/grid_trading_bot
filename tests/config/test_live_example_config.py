import json

from grid_trading_bot.config.config_manager import ConfigManager

EXAMPLE = "config/config.live.example.json"


class TestLiveExampleConfig:
    def test_example_validates_and_is_paper_trading(self):
        cm = ConfigManager(EXAMPLE)
        assert cm.config.exchange.trading_mode.value == "paper_trading"
        assert cm.is_persistence_enabled() is True
        assert cm.get_checkpoint_interval_seconds() == 60.0
        assert cm.config.risk_management.trailing_stop_loss.enabled is True
        assert cm.config.grid_strategy.dynamic_spacing.enabled is True

    def test_example_declares_maker_taker_fees_and_post_only(self):
        """
        The loader fills the fees from `trading_fee` and post_only with False, so asserting on the loaded
        values would pass with the keys absent. Read the raw file to prove the example actually ships them
        — that is what makes the options discoverable to someone copying this file.
        """
        with open(EXAMPLE) as example:
            exchange = json.load(example)["exchange"]
        # Binance spot tier 0 charges the same rate on both sides; the split is still
        # declared so the example shows where a two-tier venue would set them.
        assert exchange["maker_fee"] == 0.001
        assert exchange["taker_fee"] == 0.001
        # Grid limit orders are meant to rest on the book, and a rejection is now handled as
        # a skipped level rather than a failure, so the template ships with it enabled.
        assert exchange["post_only"] is True
        loaded = ConfigManager(EXAMPLE).config.exchange
        assert (loaded.maker_fee, loaded.taker_fee, loaded.post_only) == (0.001, 0.001, True)
