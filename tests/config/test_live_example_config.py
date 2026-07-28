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
