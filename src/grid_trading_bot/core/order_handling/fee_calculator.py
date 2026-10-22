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

    @staticmethod
    def _reported_costs(order: Order) -> dict[str, float]:
        """The order's cumulative fee per currency as the exchange reports it (one `fee` or a list of them)."""
        reported = order.fee if isinstance(order.fee, list) else [order.fee]
        costs: dict[str, float] = {}
        for fee in reported:
            if not isinstance(fee, dict) or fee.get("cost") is None or not fee.get("currency"):
                continue
            currency = str(fee["currency"])
            costs[currency] = costs.get(currency, 0.0) + abs(float(fee["cost"]))
        return costs

    def _charged(self, order: Order, costs: dict[str, float]) -> Fee:
        if self.base_currency in costs:
            return Fee(costs[self.base_currency], in_base=True)
        if self.quote_currency in costs:
            return Fee(costs[self.quote_currency], in_base=False)
        return Fee(0.0, in_base=order.side == OrderSide.BUY)

    def reported_fee(self, order: Order) -> Fee | None:
        """
        The order's cumulative fee as the exchange reports it (one `fee` or a list of them), in base or in quote.
        A fee paid in another currency (BNB) is a zero fee on both. None when the exchange reports no fee.
        """
        costs = self._reported_costs(order)
        return self._charged(order, costs) if costs else None

    def execution_fee(self, order: Order, quantity: float, price: float) -> Fee:
        """
        The fee of `quantity` more of `order` filled at `price`: the model, or once the order has ended the total
        the exchange reports less the model fee booked for its earlier fills. A report in another currency than
        those earlier fills were charged in leaves the model in place, so one order is never charged in two.
        """
        return self._delta_fee(order, quantity, price, log=True)

    def order_base_fee(self, order: Order) -> float:
        """
        The base fee booked for the whole order: the model on what it had filled before its last update, plus the
        fee of that update.
        """
        if order.side != OrderSide.BUY:
            return 0.0
        earlier = order.previously_filled * self._rate(order.liquidity)
        last = self._delta_fee(order, order.filled - order.previously_filled, order.price, log=False)
        return earlier + (last.amount if last.in_base else 0.0)

    def _delta_fee(self, order: Order, quantity: float, price: float, log: bool) -> Fee:
        model = self.fill_fee(order.side, quantity, price, order.liquidity)
        if order.status in (OrderStatus.OPEN, OrderStatus.UNKNOWN):
            return model
        costs = self._reported_costs(order)
        if not costs:
            return model
        paid = ", ".join(f"{cost} {currency}" for currency, cost in costs.items())
        booked_before = max(order.filled - quantity, 0.0)
        model_currency = self.base_currency if order.side == OrderSide.BUY else self.quote_currency
        if booked_before > 0 and set(costs) != {model_currency}:
            if log:
                self.logger.info(
                    f"Order {order.identifier} reported a fee of {paid} after fills charged in {model_currency} at "
                    "the configured rate; that rate is kept for the rest of the order."
                )
            return model
        if log and self.base_currency not in costs and self.quote_currency not in costs:
            self.logger.info(f"Order {order.identifier} paid its fee in {paid}; neither balance is charged.")
        reported = self._charged(order, costs)
        earlier = self.fill_fee(order.side, booked_before, price, order.liquidity)
        return Fee(max(reported.amount - earlier.amount, 0.0), reported.in_base)
