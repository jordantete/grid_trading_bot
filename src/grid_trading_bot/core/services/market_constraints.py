from dataclasses import dataclass


@dataclass(frozen=True)
class MarketConstraints:
    """Exchange-imposed minimums for a trading pair, from ccxt market metadata."""

    min_amount: float | None
    min_cost: float | None

    def violation(self, quantity: float, price: float) -> str | None:
        if self.min_amount is not None and quantity < self.min_amount:
            return f"quantity {quantity} is below the exchange minimum amount {self.min_amount}"
        if self.min_cost is not None and quantity * price < self.min_cost:
            return f"notional {quantity * price} is below the exchange minimum cost {self.min_cost}"
        return None
