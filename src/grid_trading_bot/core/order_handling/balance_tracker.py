import asyncio
from decimal import Decimal
import logging

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from ..validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
)
from .fee_calculator import FeeCalculator
from .order import Order, OrderSide, OrderStatus

_QUANTIZE_EXP = Decimal("1e-8")


class BalanceTracker:
    def __init__(
        self,
        event_bus: EventBus,
        fee_calculator: FeeCalculator,
        trading_mode: TradingMode,
        base_currency: str,
        quote_currency: str,
    ):
        """
        Initializes the BalanceTracker.

        Args:
            event_bus: The event bus instance for subscribing to events.
            fee_calculator: The fee calculator instance for calculating trading fees.
            trading_mode: "BACKTEST", "LIVE" or "PAPER_TRADING".
            base_currency: The base currency symbol.
            quote_currency: The quote currency symbol.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.event_bus: EventBus = event_bus
        self.fee_calculator: FeeCalculator = fee_calculator
        self.trading_mode: TradingMode = trading_mode
        self.base_currency: str = base_currency
        self.quote_currency: str = quote_currency

        self._balance: Decimal = Decimal("0")
        self._crypto_balance: Decimal = Decimal("0")
        self._total_fees: Decimal = Decimal("0")
        self._reserved_fiat: Decimal = Decimal("0")
        self._reserved_crypto: Decimal = Decimal("0")
        self._lock = asyncio.Lock()

        self.event_bus.subscribe(Events.ORDER_FILLED, self._update_balance_on_order_completion)

    @staticmethod
    def _to_decimal(value: float | int | Decimal) -> Decimal:
        return Decimal(str(value))

    def cleanup(self) -> None:
        """Unsubscribes from all EventBus events."""
        self.event_bus.unsubscribe(Events.ORDER_FILLED, self._update_balance_on_order_completion)

    @property
    def balance(self) -> float:
        return float(self._balance)

    @property
    def crypto_balance(self) -> float:
        return float(self._crypto_balance)

    @property
    def total_fees(self) -> float:
        return float(self._total_fees)

    @property
    def reserved_fiat(self) -> float:
        return float(self._reserved_fiat)

    @property
    def reserved_crypto(self) -> float:
        return float(self._reserved_crypto)

    async def setup_balances(
        self,
        initial_balance: float,
        initial_crypto_balance: float,
        exchange_service=ExchangeInterface,
    ):
        """
        Sets up the balances based on trading mode.

        For BACKTEST and PAPER_TRADING modes, sets initial balances from config.
        For LIVE mode, fetches balances dynamically from the exchange.

        Args:
            initial_balance: The initial fiat balance for backtest and paper trading modes.
            initial_crypto_balance: The initial crypto balance for backtest and paper trading modes.
            exchange_service: The exchange instance (required for live trading).
        """
        if self.trading_mode == TradingMode.BACKTEST or self.trading_mode == TradingMode.PAPER_TRADING:
            self._balance = self._to_decimal(initial_balance)
            self._crypto_balance = self._to_decimal(initial_crypto_balance)
        elif self.trading_mode == TradingMode.LIVE:
            self._balance, self._crypto_balance = await self._fetch_live_balances(exchange_service)

    async def _fetch_live_balances(
        self,
        exchange_service: ExchangeInterface,
    ) -> tuple[Decimal, Decimal]:
        """
        Fetches live balances from the exchange asynchronously.

        Args:
            exchange_service: The exchange instance.

        Returns:
            tuple: The quote and base currency balances as Decimal.
        """
        balances = await exchange_service.get_balance()

        if not balances or "free" not in balances:
            raise ValueError(f"Unexpected balance structure: {balances}")

        quote_balance = self._to_decimal(balances.get("free", {}).get(self.quote_currency, 0.0))
        base_balance = self._to_decimal(balances.get("free", {}).get(self.base_currency, 0.0))
        self.logger.info(
            f"Fetched balances - Quote: {self.quote_currency}: {quote_balance}, "
            f"Base: {self.base_currency}: {base_balance}",
        )
        return quote_balance, base_balance

    async def _update_balance_on_order_completion(self, order: Order) -> None:
        """
        Updates the account balance and crypto balance when an order is filled.

        This method is called when an `ORDER_FILLED` event is received. It determines
        whether the filled order is a buy or sell order and updates the balances
        accordingly.

        Args:
            order: The filled `Order` object containing details such as the side
                (BUY/SELL), filled quantity, and price.
        """
        async with self._lock:
            if order.side == OrderSide.BUY:
                self._update_after_buy_order_filled(order.filled, order.price)
            elif order.side == OrderSide.SELL:
                self._update_after_sell_order_filled(order.filled, order.price)

    def _update_after_buy_order_filled(
        self,
        quantity: float,
        price: float,
    ) -> None:
        """
        Updates the balances after a buy order is completed, including handling reserved funds.

        Deducts the total cost (price * quantity + fee) from the reserved fiat balance,
        releases any unused reserved fiat back to the main balance, adds the purchased
        crypto quantity to the crypto balance, and tracks the fees incurred.

        Args:
            quantity: The quantity of crypto purchased.
            price: The price at which the crypto was purchased (per unit).
        """
        d_quantity = self._to_decimal(quantity)
        d_price = self._to_decimal(price)
        fee = self._to_decimal(self.fee_calculator.calculate_fee(quantity * price))
        total_cost = (d_quantity * d_price + fee).quantize(_QUANTIZE_EXP)

        self._reserved_fiat = (self._reserved_fiat - total_cost).quantize(_QUANTIZE_EXP)
        if self._reserved_fiat < 0:
            overflow = -self._reserved_fiat
            self._balance = (self._balance - overflow).quantize(_QUANTIZE_EXP)
            self._reserved_fiat = Decimal("0")

        self._crypto_balance = (self._crypto_balance + d_quantity).quantize(_QUANTIZE_EXP)
        self._total_fees = (self._total_fees + fee).quantize(_QUANTIZE_EXP)
        self.logger.info(f"Buy order completed: {quantity} crypto purchased at {price}.")
        self._log_balance_update(price)

    def _update_after_sell_order_filled(
        self,
        quantity: float,
        price: float,
    ) -> None:
        """
        Updates the balances after a sell order is completed, including handling reserved funds.

        Deducts the sold crypto quantity from the reserved crypto balance, releases any
        unused reserved crypto back to the main crypto balance, adds the sale proceeds
        (quantity * price - fee) to the fiat balance, and tracks the fees incurred.

        Args:
            quantity: The quantity of crypto sold.
            price: The price at which the crypto was sold (per unit).
        """
        d_quantity = self._to_decimal(quantity)
        d_price = self._to_decimal(price)
        fee = self._to_decimal(self.fee_calculator.calculate_fee(quantity * price))
        sale_proceeds = (d_quantity * d_price - fee).quantize(_QUANTIZE_EXP)
        self._reserved_crypto = (self._reserved_crypto - d_quantity).quantize(_QUANTIZE_EXP)

        if self._reserved_crypto < 0:
            overflow = -self._reserved_crypto
            self._crypto_balance = (self._crypto_balance + overflow).quantize(_QUANTIZE_EXP)
            self._reserved_crypto = Decimal("0")

        self._balance = (self._balance + sale_proceeds).quantize(_QUANTIZE_EXP)
        self._total_fees = (self._total_fees + fee).quantize(_QUANTIZE_EXP)
        self.logger.info(f"Sell order completed: {quantity} crypto sold at {price}.")
        self._log_balance_update(price)

    async def update_after_initial_purchase(self, initial_order: Order):
        """
        Updates balances after an initial crypto purchase.

        Args:
            initial_order: The Order object containing details of the completed purchase.
        """
        async with self._lock:
            if initial_order.status != OrderStatus.CLOSED:
                raise ValueError(f"Order {initial_order.identifier} is not CLOSED. Cannot update balances.")

            d_filled = self._to_decimal(initial_order.filled)
            d_average = self._to_decimal(initial_order.average)

            total_cost = (d_filled * d_average).quantize(_QUANTIZE_EXP)
            fee = self._to_decimal(self.fee_calculator.calculate_fee(initial_order.amount * initial_order.average))

            self._crypto_balance = (self._crypto_balance + d_filled).quantize(_QUANTIZE_EXP)
            self._balance = (self._balance - total_cost - fee).quantize(_QUANTIZE_EXP)
            self._total_fees = (self._total_fees + fee).quantize(_QUANTIZE_EXP)
            self._log_balance_update(initial_order.average)

    async def reserve_funds_for_buy(
        self,
        amount: float,
    ) -> None:
        """
        Reserves fiat for a pending buy order.

        Args:
            amount: The amount of fiat to reserve.
        """
        async with self._lock:
            d_amount = self._to_decimal(amount)
            if self._balance < d_amount:
                raise InsufficientBalanceError(f"Insufficient fiat balance to reserve {amount}.")

            self._reserved_fiat = (self._reserved_fiat + d_amount).quantize(_QUANTIZE_EXP)
            self._balance = (self._balance - d_amount).quantize(_QUANTIZE_EXP)
            self.logger.info(f"Reserved {amount} fiat for a buy order. Remaining fiat balance: {self._balance}.")

    async def reserve_funds_for_sell(
        self,
        quantity: float,
    ) -> None:
        """
        Reserves crypto for a pending sell order.

        Args:
            quantity: The quantity of crypto to reserve.
        """
        async with self._lock:
            d_quantity = self._to_decimal(quantity)
            if self._crypto_balance < d_quantity:
                raise InsufficientCryptoBalanceError(f"Insufficient crypto balance to reserve {quantity}.")

            self._reserved_crypto = (self._reserved_crypto + d_quantity).quantize(_QUANTIZE_EXP)
            self._crypto_balance = (self._crypto_balance - d_quantity).quantize(_QUANTIZE_EXP)
            self.logger.info(
                f"Reserved {quantity} crypto for a sell order. Remaining crypto balance: {self._crypto_balance}.",
            )

    async def release_reserved_fiat(self, amount: float) -> None:
        """
        Releases reserved fiat back to available balance.

        Args:
            amount: The amount of fiat to release from reserved.
        """
        async with self._lock:
            d_amount = self._to_decimal(amount)
            if d_amount > self._reserved_fiat:
                self.logger.warning(
                    f"Attempted to release {amount} fiat but only {self._reserved_fiat} reserved. "
                    f"Releasing all reserved fiat.",
                )
                d_amount = self._reserved_fiat

            self._reserved_fiat = (self._reserved_fiat - d_amount).quantize(_QUANTIZE_EXP)
            self._balance = (self._balance + d_amount).quantize(_QUANTIZE_EXP)
            self.logger.info(f"Released {d_amount} reserved fiat. Available fiat balance: {self._balance}.")

    async def release_reserved_crypto(self, quantity: float) -> None:
        """
        Releases reserved crypto back to available balance.

        Args:
            quantity: The quantity of crypto to release from reserved.
        """
        async with self._lock:
            d_quantity = self._to_decimal(quantity)
            if d_quantity > self._reserved_crypto:
                self.logger.warning(
                    f"Attempted to release {quantity} crypto but only {self._reserved_crypto} reserved. "
                    f"Releasing all reserved crypto.",
                )
                d_quantity = self._reserved_crypto

            self._reserved_crypto = (self._reserved_crypto - d_quantity).quantize(_QUANTIZE_EXP)
            self._crypto_balance = (self._crypto_balance + d_quantity).quantize(_QUANTIZE_EXP)
            self.logger.info(
                f"Released {d_quantity} reserved crypto. Available crypto balance: {self._crypto_balance}.",
            )

    def _log_balance_update(self, price: float) -> None:
        """Logs a consistent balance snapshot after every balance-changing event."""
        fiat = self.get_adjusted_fiat_balance()
        crypto = self.get_adjusted_crypto_balance()
        total_base = round(crypto + fiat / price, 8) if price > 0 else crypto
        self.logger.info(
            f"Updated balances. Fiat balance: {fiat}, Crypto balance: {crypto}, Total base balance: {total_base}",
        )

    def get_adjusted_fiat_balance(self) -> float:
        """
        Returns the fiat balance, including reserved funds.

        Returns:
            float: The total fiat balance including reserved funds.
        """
        return float(self._balance + self._reserved_fiat)

    def get_adjusted_crypto_balance(self) -> float:
        """
        Returns the crypto balance, including reserved funds.

        Returns:
            float: The total crypto balance including reserved funds.
        """
        return float(self._crypto_balance + self._reserved_crypto)

    def get_total_balance_value(self, price: float) -> float:
        """
        Calculates the total account value in fiat, including reserved funds.

        Args:
            price: The current market price of the crypto asset.

        Returns:
            float: The total account value in fiat terms.
        """
        return self.get_adjusted_fiat_balance() + self.get_adjusted_crypto_balance() * price
