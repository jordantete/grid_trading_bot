from decimal import Decimal

from grid_trading_bot.core.order_handling.pnl_ledger import PnlLedger

D = Decimal


class TestPnlLedger:
    def test_buys_average_the_cost_including_fees(self):
        ledger = PnlLedger()

        ledger.record_buy(D("0"), D("1"), D("100"), D("1"))
        ledger.record_buy(D("1"), D("1"), D("120"), D("1"))

        assert ledger.avg_cost == D("111")  # (100 + 1 + 120 + 1) / 2
        assert ledger.realized_pnl == D("0")

    def test_sell_realizes_against_the_average_cost_net_of_fee(self):
        ledger = PnlLedger(avg_cost=D("111"))

        ledger.record_sell(D("2"), D("1"), D("130"), D("0.5"))

        assert ledger.realized_pnl == D("18.5")  # 1 * (130 - 111) - 0.5
        assert ledger.avg_cost == D("111")

    def test_selling_the_whole_position_resets_the_cost(self):
        ledger = PnlLedger(avg_cost=D("111"))

        ledger.record_sell(D("1"), D("1"), D("100"), D("0"))

        assert ledger.avg_cost == D("0")
        assert ledger.realized_pnl == D("-11")

    def test_position_of_unknown_cost_takes_the_first_price_seen(self):
        ledger = PnlLedger()

        ledger.price_seen(D("2"), D("95"))
        ledger.price_seen(D("2"), D("80"))

        assert ledger.avg_cost == D("95")
        assert ledger.unrealized_pnl(D("2"), D("100")) == D("10")

    def test_first_buy_on_a_position_of_unknown_cost_values_it_at_the_fill_price(self):
        ledger = PnlLedger()

        ledger.record_buy(D("1"), D("1"), D("100"), D("0"))

        assert ledger.avg_cost == D("100")

    def test_empty_position_has_no_unrealized_pnl(self):
        assert PnlLedger().unrealized_pnl(D("0"), D("100")) == D("0")
