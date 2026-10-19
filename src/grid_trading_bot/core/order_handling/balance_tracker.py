import asyncio
from dataclasses import dataclass
from decimal import Decimal
import logging

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from ..validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
)
from .fee_calculator import FeeCalculator
from .order import Liquidity, Order, OrderSide, OrderStatus
from .pnl_ledger import PnlLedger

_QUANTIZE_EXP = Decimal("1e-8")


@dataclass(frozen=True)
class AccountSnapshot:
    price: float
    value: float
    peak: float
    drawdown_pct: float
    realized_pnl: float
    unrealized_pnl: float


class BalanceTracker:
    def __init__(
        self,
        fee_calculator: FeeCalculator,
        trading_mode: TradingMode,
        base_currency: str,
        quote_currency: str,
    ):
        """
        Initializes the BalanceTracker.

        Args:
            fee_calculator: The fee calculator instance for calculating trading fees.
            trading_mode: "BACKTEST", "LIVE" or "PAPER_TRADING".
            base_currency: The base currency symbol.
            quote_currency: The quote currency symbol.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.fee_calculator: FeeCalculator = fee_calculator
        self.trading_mode: TradingMode = trading_mode
        self.base_currency: str = base_currency
        self.quote_currency: str = quote_currency

        self._balance: Decimal = Decimal("0")
        self._crypto_balance: Decimal = Decimal("0")
        self._total_fees: Decimal = Decimal("0")
        self._reserved_fiat: Decimal = Decimal("0")
        self._reserved_crypto: Decimal = Decimal("0")
        self._ledger = PnlLedger()
        self._peak_value: Decimal | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _to_decimal(value: float | int | Decimal) -> Decimal:
        return Decimal(str(value))

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
        self._reserved_fiat = Decimal("0")
        self._reserved_crypto = Decimal("0")
        self._ledger = PnlLedger()
        self._peak_value = None

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

    async def record_execution(self, order: Order, filled_quantity: float) -> None:
        """
        Books `filled_quantity` more of `order` at its fill price, then releases the reservation of the unfilled
        remainder once the order is no longer open.
        """
        async with self._lock:
            fill_price = order.average if order.average is not None else order.price
            if filled_quantity > 0:
                if order.side == OrderSide.BUY:
                    self._update_after_buy_order_filled(filled_quantity, fill_price, order.liquidity)
                else:
                    self._update_after_sell_order_filled(filled_quantity, fill_price, order.liquidity)
            if order.status != OrderStatus.OPEN and order.remaining:
                if order.side == OrderSide.BUY:
                    self._release_fiat_unlocked(order.remaining * order.price)
                else:
                    self._release_crypto_unlocked(order.remaining)

    def _update_after_buy_order_filled(
        self,
        quantity: float,
        price: float,
        liquidity: Liquidity,
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
        fee = self._to_decimal(self.fee_calculator.calculate_fee(quantity * price, liquidity))
        total_cost = (d_quantity * d_price + fee).quantize(_QUANTIZE_EXP)
        self._ledger.record_buy(self._position(), d_quantity, d_price, fee)

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
        liquidity: Liquidity,
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
        fee = self._to_decimal(self.fee_calculator.calculate_fee(quantity * price, liquidity))
        sale_proceeds = (d_quantity * d_price - fee).quantize(_QUANTIZE_EXP)
        self._ledger.record_sell(self._position(), d_quantity, d_price, fee)
        self._reserved_crypto = (self._reserved_crypto - d_quantity).quantize(_QUANTIZE_EXP)

        if self._reserved_crypto < 0:
            overflow = -self._reserved_crypto
            self._crypto_balance = (self._crypto_balance - overflow).quantize(_QUANTIZE_EXP)
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
            fee = self._to_decimal(
                self.fee_calculator.calculate_fee(
                    initial_order.filled * initial_order.average, initial_order.liquidity
                ),
            )
            self._ledger.record_buy(self._position(), d_filled, d_average, fee)

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

    def _release_fiat_unlocked(self, amount: float) -> None:
        """Sync body of `release_reserved_fiat`, callable from within a held lock."""
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

    async def release_reserved_fiat(self, amount: float) -> None:
        """
        Releases reserved fiat back to available balance.

        Args:
            amount: The amount of fiat to release from reserved.
        """
        async with self._lock:
            self._release_fiat_unlocked(amount)

    def _release_crypto_unlocked(self, quantity: float) -> None:
        """Sync body of `release_reserved_crypto`, callable from within a held lock."""
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

    async def release_reserved_crypto(self, quantity: float) -> None:
        """
        Releases reserved crypto back to available balance.

        Args:
            quantity: The quantity of crypto to release from reserved.
        """
        async with self._lock:
            self._release_crypto_unlocked(quantity)

    async def update_after_liquidation(self, order: Order) -> None:
        """
        Updates balances after a liquidation market sell (TP/SL/trailing/shutdown).
        Mirror of update_after_initial_purchase: closed market orders never enter the
        open-order tracking, so no fill update ever reaches the order pipeline for them.

        Args:
            order: The CLOSED market SELL order.
        """
        async with self._lock:
            if order.status != OrderStatus.CLOSED:
                raise ValueError(f"Order {order.identifier} is not CLOSED. Cannot update balances.")

            fill_price = order.average if order.average is not None else order.price
            d_filled = self._to_decimal(order.filled)
            d_price = self._to_decimal(fill_price)
            fee = self._to_decimal(self.fee_calculator.calculate_fee(order.filled * fill_price, order.liquidity))
            proceeds = (d_filled * d_price - fee).quantize(_QUANTIZE_EXP)
            self._ledger.record_sell(self._position(), d_filled, d_price, fee)

            self._crypto_balance = (self._crypto_balance - d_filled).quantize(_QUANTIZE_EXP)
            if self._crypto_balance < 0:
                self._crypto_balance = Decimal("0")

            self._balance = (self._balance + proceeds).quantize(_QUANTIZE_EXP)
            self._total_fees = (self._total_fees + fee).quantize(_QUANTIZE_EXP)
            self._log_balance_update(fill_price)

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

    def _position(self) -> Decimal:
        return self._crypto_balance + self._reserved_crypto

    def snapshot(self, price: float) -> AccountSnapshot:
        """Values the account at `price` and raises the recorded peak when the value exceeds it."""
        d_price = self._to_decimal(price)
        position = self._position()
        self._ledger.price_seen(position, d_price)
        value = self._balance + self._reserved_fiat + position * d_price
        if self._peak_value is None or value > self._peak_value:
            self._peak_value = value
        drawdown_pct = (self._peak_value - value) / self._peak_value * 100 if self._peak_value > 0 else Decimal("0")
        return AccountSnapshot(
            price=price,
            value=float(value),
            peak=float(self._peak_value),
            drawdown_pct=float(drawdown_pct),
            realized_pnl=float(self._ledger.realized_pnl),
            unrealized_pnl=float(self._ledger.unrealized_pnl(position, d_price)),
        )

    def restore_accounting(self, avg_cost: Decimal | None, realized_pnl: Decimal, peak_value: Decimal | None) -> None:
        self._ledger = PnlLedger(avg_cost=avg_cost, realized_pnl=realized_pnl)
        self._peak_value = peak_value

    def replay_fills_on_cost_basis(self, starting_position: Decimal, executions: list[tuple[Order, float]]) -> None:
        """
        Books the quantities the exchange filled while the bot was down into the cost basis, the realized PnL and the
        fees. Balances stay untouched: they were restored from the exchange, which already includes these fills.
        """
        position = starting_position
        fills = [(order, quantity) for order, quantity in executions if quantity > 0]
        for order, quantity in sorted(fills, key=lambda item: item[0].last_trade_timestamp or item[0].timestamp or 0):
            fill_price = order.average if order.average is not None else order.price
            d_quantity = self._to_decimal(quantity)
            d_price = self._to_decimal(fill_price)
            fee = self._to_decimal(self.fee_calculator.calculate_fee(quantity * fill_price, order.liquidity))
            if order.side == OrderSide.BUY:
                self._ledger.record_buy(position, d_quantity, d_price, fee)
                position += d_quantity
            else:
                self._ledger.record_sell(position, d_quantity, d_price, fee)
                position -= d_quantity
            self._total_fees = (self._total_fees + fee).quantize(_QUANTIZE_EXP)
