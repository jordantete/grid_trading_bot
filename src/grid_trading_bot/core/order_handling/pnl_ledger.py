from dataclasses import dataclass
from decimal import Decimal

_COST_QUANTIZE = Decimal("1e-12")
_PNL_QUANTIZE = Decimal("1e-8")


@dataclass
class PnlLedger:
    """Weighted average cost of the crypto position and cumulative realized PnL, in quote currency."""

    avg_cost: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")

    def price_seen(self, position: Decimal, price: Decimal) -> Decimal:
        """Gives a position of unknown cost the first price observed, so its unrealized PnL starts at 0."""
        if self.avg_cost is None:
            self.avg_cost = price if position > 0 else Decimal("0")
        return self.avg_cost

    def record_buy(self, position: Decimal, quantity: Decimal, price: Decimal, fee: Decimal) -> None:
        avg_cost = self.price_seen(position, price)
        new_position = position + quantity
        if new_position <= 0:
            self.avg_cost = Decimal("0")
            return
        self.avg_cost = ((position * avg_cost + quantity * price + fee) / new_position).quantize(_COST_QUANTIZE)

    def record_sell(self, position: Decimal, quantity: Decimal, price: Decimal, fee: Decimal) -> None:
        avg_cost = self.price_seen(position, price)
        self.realized_pnl = (self.realized_pnl + quantity * (price - avg_cost) - fee).quantize(_PNL_QUANTIZE)
        if position - quantity <= 0:
            self.avg_cost = Decimal("0")

    def unrealized_pnl(self, position: Decimal, price: Decimal) -> Decimal:
        if self.avg_cost is None or position <= 0:
            return Decimal("0")
        return position * (price - self.avg_cost)
