from collections.abc import Awaitable, Callable
from decimal import Decimal
import logging

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.order_handling.balance_tracker import _QUANTIZE_EXP, BalanceTracker
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderType
from grid_trading_bot.core.order_handling.order_book import OrderBook
from grid_trading_bot.core.order_handling.placement_journal import PlacementJournal
from grid_trading_bot.core.services.exchange_interface import ExchangeInterface

from .state_repository_interface import SavedBalance, StateRepositoryInterface


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _quantized(value: Decimal) -> Decimal:
    return value.quantize(_QUANTIZE_EXP)


class BalanceRestore:
    """Restores the balances, the reservations and the accounting of a recovered bot into its tracker."""

    def __init__(
        self,
        repository: StateRepositoryInterface,
        order_book: OrderBook,
        balance_tracker: BalanceTracker,
        exchange_service: ExchangeInterface,
        journal: PlacementJournal,
        base_currency: str,
        quote_currency: str,
        notify: Callable[..., Awaitable[None]],
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.repository = repository
        self.order_book = order_book
        self.balance_tracker = balance_tracker
        self.exchange_service = exchange_service
        self.journal = journal
        self.base_currency = base_currency
        self.quote_currency = quote_currency
        self._notify = notify

    @staticmethod
    def _reservations(held: list[tuple[OrderSide, float, float]]) -> tuple[Decimal, Decimal]:
        """
        What `held` ((side, quantity, price)) needs in reserve: (fiat, crypto). Each amount is taken at the tracker's
        precision, the way the release that later frees it takes it.
        """
        fiat = sum(
            (_quantized(Decimal(str(quantity * price))) for side, quantity, price in held if side == OrderSide.BUY),
            Decimal("0"),
        )
        crypto = sum(
            (_quantized(Decimal(str(quantity))) for side, quantity, _ in held if side == OrderSide.SELL),
            Decimal("0"),
        )
        return fiat, crypto

    def _entry_reservations(self) -> tuple[Decimal, Decimal]:
        """What the pending limit placements hold in reserve: (fiat, crypto)."""
        return self._reservations([(e.side, e.quantity, e.limit_price) for e in self.journal.limit_entries()])

    def _needed_reservations(self) -> tuple[Decimal, Decimal]:
        """What the open orders and the pending limit placements need in reserve: (fiat, crypto)."""
        held = [(o.side, o.remaining, o.price) for o in self.order_book.get_open_orders()]
        held += [(e.side, e.quantity, e.limit_price) for e in self.journal.limit_entries()]
        return self._reservations(held)

    def _resplit_reservations(self) -> None:
        """Reserves exactly what the open orders and the pending placements need, keeping the restored totals."""
        fiat_total, crypto_total = self.balance_tracker.totals()
        reserved_fiat, reserved_crypto = self._needed_reservations()
        self.balance_tracker.restore_balances(
            fiat=fiat_total - reserved_fiat,
            crypto=crypto_total - reserved_crypto,
            reserved_fiat=reserved_fiat,
            reserved_crypto=reserved_crypto,
        )

    async def restore_from_database(self, executions: list[tuple[Order, float]]) -> None:
        """
        A virtual balance is not the exchange account's: it resumes from the database,
        then books every execution seen at restart like a live fill.
        """
        saved_balance = self.repository.load_balance_state()
        if saved_balance is None:
            raise ValueError("The database holds a bot state without its balance state.")
        tracker = self.balance_tracker
        tracker.restore_balances(
            fiat=Decimal(saved_balance["fiat_balance"]),
            crypto=Decimal(saved_balance["crypto_balance"]),
            reserved_fiat=Decimal(saved_balance["reserved_fiat"]),
            reserved_crypto=Decimal(saved_balance["reserved_crypto"]),
            total_fees=Decimal(saved_balance["total_fees"]),
        )
        try:
            self._restore_saved_accounting(saved_balance)
        except Exception as e:
            self.logger.error(f"Failed to restore cost basis and PnL, starting them over: {e}", exc_info=True)
            await self.alert_accounting_not_restored(e)
        # A market order reserved nothing, so it is booked like the purchase or the liquidation it was part of.
        for order, quantity in executions:
            if order.order_type is not OrderType.MARKET:
                await tracker.record_execution(order, quantity)
            elif order.side is OrderSide.BUY:
                await tracker.update_after_initial_purchase(order)
            else:
                await tracker.update_after_liquidation(order)
        # A placement written after the last full save is in the journal but not in the saved reservations.
        self._resplit_reservations()
        self.logger.info(f"Balance restored from the database with {len(executions)} execution(s) booked.")

    async def restore_from_exchange(self) -> None:
        """Takes the free balances from the exchange and the fees from the database, and reserves what is held."""
        saved_balance = self.repository.load_balance_state()

        exchange_balance = await self.exchange_service.get_balance()
        exchange_fiat = Decimal(str(exchange_balance.get("free", {}).get(self.quote_currency, 0)))
        exchange_crypto = Decimal(str(exchange_balance.get("free", {}).get(self.base_currency, 0)))

        # Exchange "free" excludes the funds of its open orders but still holds those of the pending placements,
        # which the bot reserves.
        reserved_fiat, reserved_crypto = self._needed_reservations()
        entry_fiat, entry_crypto = self._entry_reservations()
        self.balance_tracker.restore_balances(
            fiat=exchange_fiat - entry_fiat,
            crypto=exchange_crypto - entry_crypto,
            reserved_fiat=reserved_fiat,
            reserved_crypto=reserved_crypto,
            # Total fees from DB (exchange doesn't track cumulative fees)
            total_fees=Decimal(saved_balance["total_fees"]) if saved_balance else None,
        )

        self.logger.info(
            f"Balance restored from exchange: "
            f"fiat={self.balance_tracker.balance}, "
            f"crypto={self.balance_tracker.crypto_balance}, "
            f"reserved_fiat={self.balance_tracker.reserved_fiat}, "
            f"reserved_crypto={self.balance_tracker.reserved_crypto}"
        )

    def _restore_saved_accounting(self, saved_balance: SavedBalance) -> None:
        self.balance_tracker.restore_accounting(
            avg_cost=_optional_decimal(saved_balance.get("avg_cost")),
            realized_pnl=Decimal(saved_balance.get("realized_pnl") or "0"),
            peak_value=_optional_decimal(saved_balance.get("peak_value")),
        )

    def restore_accounting(self, executions: list[tuple[Order, float]]) -> None:
        """Restores the saved cost basis, realized PnL and peak, then replays on them the fills made while down."""
        saved_balance = self.repository.load_balance_state()
        if not saved_balance:
            return

        self._restore_saved_accounting(saved_balance)
        if executions:
            starting_position = Decimal(saved_balance["crypto_balance"]) + Decimal(saved_balance["reserved_crypto"])
            self.balance_tracker.replay_fills_on_cost_basis(starting_position, executions)

    async def alert_accounting_not_restored(self, error: Exception) -> None:
        """Alerts that the cost basis and PnL were lost during the recovery; a failed alert is only logged."""
        try:
            await self._notify(NotificationType.ACCOUNTING_NOT_RESTORED, error=f"{type(error).__name__}: {error}")
        except Exception as e:
            self.logger.warning(f"Failed to send the alert about the lost cost basis and PnL: {e}")
