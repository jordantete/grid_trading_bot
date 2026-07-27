from grid_trading_bot.core.services.market_constraints import MarketConstraints


class TestMarketConstraints:
    def test_no_violation_when_above_minimums(self):
        c = MarketConstraints(min_amount=0.01, min_cost=5.0)
        assert c.violation(quantity=0.1, price=100.0) is None

    def test_violation_below_min_amount(self):
        c = MarketConstraints(min_amount=0.01, min_cost=None)
        assert "minimum amount" in c.violation(quantity=0.001, price=100.0)

    def test_violation_below_min_cost(self):
        c = MarketConstraints(min_amount=None, min_cost=10.0)
        assert "minimum cost" in c.violation(quantity=0.05, price=100.0)

    def test_none_limits_never_violate(self):
        c = MarketConstraints(min_amount=None, min_cost=None)
        assert c.violation(quantity=1e-12, price=0.0001) is None
