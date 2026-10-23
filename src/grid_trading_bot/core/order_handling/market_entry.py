from collections.abc import Awaitable, Callable
import copy
import time

from grid_trading_bot.core.exceptions import OrderRefusedError
from grid_trading_bot.core.services.exceptions import (
    OrderEndedUnfilledError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
)

from .execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
from .order import FINAL_STATUSES, Order, OrderSide, OrderStatus, OrderType
from .placement_journal import PendingPlacement, PlacementJournal, PlacementPurpose, new_client_order_id


class MarketEntrySender:
    """Journals market entries and sends them, looking up a leg whose earlier outcome is unknown first."""

    def __init__(
        self,
        journal: PlacementJournal,
        order_execution_strategy: OrderExecutionStrategyInterface,
        trading_pair: str,
        config_hash: str,
        save_pending: Callable[[PendingPlacement], Awaitable[bool]],
    ):
        self.journal = journal
        self.order_execution_strategy = order_execution_strategy
        self.trading_pair = trading_pair
        self.config_hash = config_hash
        self._save_pending = save_pending

    def new_entry(self, side: OrderSide, quantity: float, price: float, purpose: PlacementPurpose) -> PendingPlacement:
        entry = PendingPlacement(
            client_order_id=new_client_order_id(self.config_hash),
            order_type=OrderType.MARKET,
            side=side,
            quantity=self.order_execution_strategy.round_amount(self.trading_pair, quantity),
            price=price,
            purpose=purpose,
            created_at=int(time.time() * 1000),
        )
        self.journal.add(entry)
        return entry

    async def send(self, entry: PendingPlacement) -> Order:
        """
        Sends one market leg through the journal, looking it up first when an earlier send's outcome is unknown.
        A found order is returned only once it is closed and filled; one that ended, closed included, without a fill has
        taken its client id, so the entry moves to a new one. A lookup counts as an attempt.
        Raises DataFetchError when the order is not placed or its outcome stays unknown, OrderRefusedError on a refusal.
        Every raise leaves the entry in the journal, unsent under a client id no order took, unless it may be live.
        """
        if entry.price is None:
            raise ValueError(f"Market placement {entry.client_order_id} has no reference price.")
        if entry.sent:
            entry.attempts += 1
            found = await self.order_execution_strategy.find_order(entry.client_order_id, self.trading_pair)
            if found is not None and found.status == OrderStatus.OPEN:
                raise OrderOutcomeUnknownError(f"Market order {entry.client_order_id} is still open on the exchange.")
            if found is not None and found.filled:
                settled = copy.copy(found)
                settled.status = OrderStatus.CLOSED
                settled.average = found.average or found.price or entry.price
                return settled
            if found is not None and found.status not in FINAL_STATUSES:
                raise OrderOutcomeUnknownError(
                    f"Market order {entry.client_order_id} is {found.status.value} on the exchange without a fill."
                )
            if found is not None:
                self._move_to_new_client_id(entry)
            entry.sent = False
        entry.sent = True
        if not await self._save_pending(entry):
            entry.sent = False
            raise OrderNotPlacedError(f"Placement {entry.client_order_id} could not be saved before sending.")
        entry.attempts += 1
        try:
            order = await self.order_execution_strategy.execute_market_order(
                entry.side, self.trading_pair, entry.quantity, entry.price, entry.client_order_id
            )
        except OrderNotPlacedError:
            entry.sent = False
            raise
        except (OrderEndedUnfilledError, OrderRefusedError) as e:
            self._move_to_new_client_id(entry)
            if isinstance(e, OrderRefusedError):
                raise
            raise OrderNotPlacedError(f"{e} Next attempt under client id {entry.client_order_id}.") from e
        if not order.filled:
            taken = entry.client_order_id
            self._move_to_new_client_id(entry)
            raise OrderNotPlacedError(f"Market order {taken} closed without a fill.")
        return order

    def _move_to_new_client_id(self, entry: PendingPlacement) -> None:
        """Re-keys, unsent, an entry whose client id an order that executed nothing may already hold."""
        self.journal.remove(entry.client_order_id)
        entry.client_order_id = new_client_order_id(self.config_hash)
        entry.sent = False
        entry.attempts = 0
        self.journal.add(entry)
