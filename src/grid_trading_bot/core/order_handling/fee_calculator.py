from dataclasses import dataclass
import logging

from grid_trading_bot.config.config_manager import ConfigManager

from .order import Liquidity, Order, OrderSide, OrderStatus


@dataclass(frozen=True)
class Fee:
    amount: float
    in_base: bool


class FeeCalculator:
    """
    Charges each fill at the rate matching its liquidity role, in the currency the fill receives: a buy pays in
    base, a sell in quote, as spot exchanges charge without a fee token.

    Both rates fall back to the flat `exchange.trading_fee` when `maker_fee` / `taker_fee`
    are not configured, so an existing single-rate config keeps behaving exactly as before.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
    ):
        self.config_manager = config_manager
        self.logger = logging.getLogger(self.__class__.__name__)
        self.trading_fee: float = self.config_manager.get_trading_fee()
        self.maker_fee: float = self.config_manager.get_maker_fee()
        self.taker_fee: float = self.config_manager.get_taker_fee()
        self.base_currency = self.config_manager.get_base_currency()
        self.quote_currency = self.config_manager.get_quote_currency()

    def _rate(self, liquidity: Liquidity) -> float:
        return self.maker_fee if liquidity == Liquidity.MAKER else self.taker_fee

    def calculate_fee(
        self,
        trade_value: float,
        liquidity: Liquidity,
    ) -> float:
        return trade_value * self._rate(liquidity)

    def fill_fee(self, side: OrderSide, quantity: float, price: float, liquidity: Liquidity) -> Fee:
        """The fee of one fill under the configured rates: a buy pays in base, a sell in quote."""
        rate = self._rate(liquidity)
        if side == OrderSide.BUY:
            return Fee(quantity * rate, in_base=True)
        return Fee(quantity * price * rate, in_base=False)

    def reported_fee(self, order: Order) -> Fee | None:
        """
        The order's cumulative fee as the exchange reports it (one `fee` or a list of them), in base or in quote.
        A fee paid in another currency (BNB) is a zero fee on both. None when the exchange reports no fee.
        """
        reported = order.fee if isinstance(order.fee, list) else [order.fee]
        costs: dict[str, float] = {}
        for fee in reported:
            if not isinstance(fee, dict) or fee.get("cost") is None or not fee.get("currency"):
                continue
            costs[fee["currency"]] = costs.get(fee["currency"], 0.0) + abs(float(fee["cost"]))
        if not costs:
            return None
        if self.base_currency in costs:
            return Fee(costs[self.base_currency], in_base=True)
        if self.quote_currency in costs:
            return Fee(costs[self.quote_currency], in_base=False)
        paid = ", ".join(f"{cost} {currency}" for currency, cost in costs.items())
        self.logger.info(f"Order {order.identifier} paid its fee in {paid}; neither balance is charged.")
        return Fee(0.0, in_base=order.side == OrderSide.BUY)

    def execution_fee(self, order: Order, quantity: float, price: float) -> Fee:
        """
        The fee of `quantity` more of `order` filled at `price`: the model, or once the order has ended the total
        the exchange reports less the model fee already booked for its earlier fills.
        """
        model = self.fill_fee(order.side, quantity, price, order.liquidity)
        if order.status in (OrderStatus.OPEN, OrderStatus.UNKNOWN):
            return model
        reported = self.reported_fee(order)
        if reported is None:
            return model
        earlier = self.fill_fee(order.side, max(order.filled - quantity, 0.0), price, order.liquidity)
        booked = earlier.amount if earlier.in_base == reported.in_base else 0.0
        return Fee(max(reported.amount - booked, 0.0), reported.in_base)

    def order_base_fee(self, order: Order) -> float:
        """The base fee of the whole order: what the exchange reports once it ended, else the model on its fill."""
        if order.side != OrderSide.BUY:
            return 0.0
        if order.status not in (OrderStatus.OPEN, OrderStatus.UNKNOWN):
            reported = self.reported_fee(order)
            if reported is not None:
                return reported.amount if reported.in_base else 0.0
        return order.filled * self._rate(order.liquidity)
