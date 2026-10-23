import asyncio
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.fakes.recording_store import RecordingStore
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.placement_journal import PlacementPurpose
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    OrderNotPlacedError,
    OrderRejectedError,
    PostOnlyRejectedError,
)
from grid_trading_bot.core.validation.exceptions import InsufficientCryptoBalanceError


@pytest.fixture
async def stack():
    stack = build_stack()
    await stack.manager.initialize_grid_orders(ARMING_PRICE)
    return stack


def _observing_store(stack) -> RecordingStore:
    """A store that records, at each save, the open orders of the book, whether the order lock was held and whether
    the reserved funds equal the open orders."""
    return RecordingStore(
        observe=lambda: SimpleNamespace(
            open_ids={o.identifier for o in stack.book.get_open_orders()},
            locked=stack.manager._lock.locked(),
            consistent=reserved_matches_open_orders(stack),
        )
    )


def _sent(stack):
    return [call.args[0] for call in stack.notifications.async_send_notification.await_args_list]


class TestFills:
    async def test_a_filled_buy_books_the_fill_frees_its_level_and_places_the_sell(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        local = stack.book.get_order(buy.identifier)
        assert (local.is_filled(), local.filled) == (True, buy.amount)
        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.999)
        assert stack.grid.grid_levels[100.0].state == GridCycleState.READY_TO_BUY
        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        assert sell.amount == pytest.approx(buy.amount * 0.999)
        assert stack.book.get_order(sell.identifier).is_open()
        assert stack.grid.grid_levels[110.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert reserved_matches_open_orders(stack)

    async def test_a_filled_sell_books_the_proceeds_and_places_the_rebuy(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))
        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        fiat_before = stack.balances.get_adjusted_fiat_balance()

        await stack.manager.apply_order_update(stack.execution.fill(sell.identifier))

        assert stack.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_before + sell.amount * 110.0 * 0.999)
        assert stack.execution.order_at(100.0, OrderSide.BUY).amount == pytest.approx(sell.amount)
        assert reserved_matches_open_orders(stack)

    async def test_a_closed_update_without_a_filled_quantity_books_the_full_amount(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        view = stack.execution.fill(buy.identifier)
        view.filled = None
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(view)

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.999)
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount * 0.999)
        assert reserved_matches_open_orders(stack)

    async def test_a_closed_update_with_a_remainder_releases_its_reservation(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        view = stack.execution.fill(buy.identifier, buy.amount * 0.4)
        view.status = OrderStatus.CLOSED
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(view)

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.4 * 0.999)
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount * 0.4 * 0.999)
        assert reserved_matches_open_orders(stack)

    async def test_an_update_for_an_order_no_longer_open_is_ignored(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        update = stack.execution.fill(buy.identifier)
        await stack.manager.apply_order_update(update)
        crypto_after_first = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(update)

        assert stack.balances.get_adjusted_crypto_balance() == crypto_after_first
        assert len(stack.execution.open_orders(OrderSide.SELL)) == 2

    async def test_an_update_for_an_unknown_order_is_ignored(self, stack):
        stranger = await stack.execution.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 95.0, "gstranger")
        balances_before = (stack.balances.balance, stack.balances.crypto_balance)

        await stack.manager.apply_order_update(stack.execution.fill(stranger.identifier))

        assert (stack.balances.balance, stack.balances.crypto_balance) == balances_before

    async def test_only_the_quantity_filled_since_the_last_view_is_booked(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        # Booked before, as recovery records a partial fill seen at restart.
        stack.book.get_order(buy.identifier).filled = buy.amount * 0.4
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.6 * 0.999)

    async def test_a_fill_after_the_positions_closed_is_booked_without_a_paired_order(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.manager._positions_closed = True
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.999)
        assert stack.execution.open_orders(OrderSide.SELL) == [stack.execution.order_at(120.0, OrderSide.SELL)]

    async def test_a_fill_without_a_grid_level_is_booked_and_places_nothing(self, stack):
        loose = await stack.execution.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 95.0, "gloose")
        stack.book.add_order(loose)
        crypto_before = stack.balances.get_adjusted_crypto_balance()
        orders_before = len(stack.execution.orders)

        await stack.manager.apply_order_update(stack.execution.fill(loose.identifier))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + 0.999)
        assert len(stack.execution.orders) == orders_before

    async def test_a_failed_paired_placement_keeps_the_fill_booked_and_saves(self, stack):
        store = _observing_store(stack)
        stack.manager.state_store = store
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.fail_next_limit_order = OrderRejectedError("network down")
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.999)
        assert stack.grid.grid_levels[100.0].state == GridCycleState.READY_TO_BUY
        assert stack.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert [save.consistent for save in store.observed] == [True]
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_an_unexpected_pairing_error_in_live_keeps_the_fill_booked_alerts_and_saves(self, stack):
        store = _observing_store(stack)
        stack.manager.state_store = store
        sent = []

        async def record(content, **_kwargs):
            sent.append((content, stack.manager._lock.locked()))

        stack.notifications.async_send_notification.side_effect = record
        stack.grid.complete_order = Mock(side_effect=RuntimeError("boom"))
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        crypto_before = stack.balances.get_adjusted_crypto_balance()
        orders_before = len(stack.execution.orders)

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert stack.book.get_order(buy.identifier).is_filled()
        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.999)
        assert len(stack.execution.orders) == orders_before
        assert len(stack.journal) == 0
        assert len(store.saves) == 1
        assert (NotificationType.ERROR_OCCURRED, False) in sent

    async def test_an_unexpected_pairing_error_fails_a_backtest(self):
        stack = build_stack(trading_mode="backtest")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        stack.grid.complete_order = Mock(side_effect=RuntimeError("boom"))
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        with pytest.raises(RuntimeError, match="boom"):
            await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

    @pytest.mark.parametrize("error", [OrderNotPlacedError("exchange down"), DataFetchError("exchange unavailable")])
    async def test_a_paired_placement_the_exchange_cannot_take_yet_waits_in_the_journal(self, stack, error):
        store = _observing_store(stack)
        stack.manager.state_store = store
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.fail_next_limit_order = error

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        [entry] = stack.journal.limit_entries()
        assert (entry.side, entry.level_price) == (OrderSide.SELL, 110.0)
        assert stack.grid.grid_levels[110.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert [save.consistent for save in store.observed] == [True]
        assert NotificationType.ORDER_PENDING in _sent(stack)

    async def test_a_paired_order_the_validator_refuses_keeps_the_fill_booked_and_saves(self, stack):
        store = _observing_store(stack)
        stack.manager.state_store = store
        stack.manager.order_validator.adjust_and_validate_sell_quantity = Mock(
            side_effect=InsufficientCryptoBalanceError("no crypto")
        )
        held_when_sent = []

        async def record(content, **_kwargs):
            if content is NotificationType.ORDER_FILLED:
                held_when_sent.append(stack.manager._lock.locked())

        stack.notifications.async_send_notification.side_effect = record
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.999)
        assert stack.grid.grid_levels[100.0].state == GridCycleState.READY_TO_BUY
        assert stack.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert [save.consistent for save in store.observed] == [True]
        assert held_when_sent == [False]
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_the_state_is_saved_under_the_lock_once_the_paired_order_exists(self, stack):
        store = _observing_store(stack)
        stack.manager.state_store = store
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        assert len(store.observed) == 1
        assert sell.identifier in store.observed[0].open_ids
        assert (store.observed[0].locked, store.observed[0].consistent) == (True, True)

    async def test_updates_applied_together_are_saved_one_consistent_state_at_a_time(self, stack):
        store = _observing_store(stack)
        stack.manager.state_store = store
        stack.execution.placement_delay = 0.01
        buy_100 = stack.execution.order_at(100.0, OrderSide.BUY)
        buy_90 = stack.execution.order_at(90.0, OrderSide.BUY)

        await asyncio.gather(
            stack.manager.apply_order_update(stack.execution.fill(buy_100.identifier)),
            stack.manager.apply_order_update(stack.execution.fill(buy_90.identifier)),
        )

        assert [(save.locked, save.consistent) for save in store.observed] == [(True, True), (True, True)]

    async def test_the_fill_notification_goes_out_after_the_lock_is_released(self, stack):
        held_when_sent = []

        async def record(content, **_kwargs):
            if content is NotificationType.ORDER_FILLED:
                held_when_sent.append(stack.manager._lock.locked())

        stack.notifications.async_send_notification.side_effect = record
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert held_when_sent == [False]

    async def test_the_paired_sell_is_sized_on_the_crypto_the_fill_received(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        view = stack.execution.fill(buy.identifier)
        view.fee = {"cost": buy.amount * 0.003, "currency": "BTC"}
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(view)

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.997)
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount * 0.997)
        assert reserved_matches_open_orders(stack)

    async def test_a_fee_paid_in_another_currency_leaves_the_whole_fill_to_sell(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        view = stack.execution.fill(buy.identifier)
        view.fee = {"cost": 0.01, "currency": "BNB"}

        await stack.manager.apply_order_update(view)

        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount)


class TestEndedOrders:
    @pytest.mark.parametrize("status", [OrderStatus.CANCELED, OrderStatus.EXPIRED])
    async def test_an_ended_order_releases_its_funds_frees_its_level_and_is_replaced(self, stack, status):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, status))

        replacement = stack.execution.order_at(90.0, OrderSide.BUY)
        assert replacement.identifier != buy.identifier
        assert replacement.amount == pytest.approx(buy.amount)
        assert stack.book.get_order(buy.identifier).status == status
        assert stack.grid.grid_levels[90.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_CANCELLED in _sent(stack)

    async def test_a_rejected_order_is_released_and_not_replaced(self, stack):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, OrderStatus.REJECTED))

        assert sorted(o.price for o in stack.execution.open_orders(OrderSide.BUY)) == [80.0, 100.0]
        assert stack.grid.grid_levels[90.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ERROR_OCCURRED in _sent(stack)

    async def test_a_partly_filled_cancelled_order_books_its_fill_and_replaces_the_rest(self, stack):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)
        stack.execution.fill(buy.identifier, buy.amount * 0.4)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, OrderStatus.CANCELED))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.4 * 0.999)
        assert stack.execution.order_at(90.0, OrderSide.BUY).amount == pytest.approx(buy.amount * 0.6)
        assert reserved_matches_open_orders(stack)

    async def test_an_ended_order_after_the_positions_closed_is_settled_not_replaced(self, stack):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)
        stack.manager._positions_closed = True

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, OrderStatus.CANCELED))

        assert sorted(o.price for o in stack.execution.open_orders(OrderSide.BUY)) == [80.0, 100.0]
        assert reserved_matches_open_orders(stack)

    async def test_an_ended_order_without_a_filled_quantity_books_nothing(self, stack):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)
        view = stack.execution.end(buy.identifier, OrderStatus.CANCELED)
        view.filled = None
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(view)

        assert stack.balances.get_adjusted_crypto_balance() == crypto_before
        assert reserved_matches_open_orders(stack)

    async def test_an_ended_order_on_a_stacked_level_keeps_the_level_waiting(self, stack):
        await stack.manager.apply_order_update(
            stack.execution.fill(stack.execution.order_at(100.0, OrderSide.BUY).identifier)
        )
        await stack.manager.apply_order_update(
            stack.execution.fill(stack.execution.order_at(90.0, OrderSide.BUY).identifier)
        )
        first_sell = next(o for o in stack.execution.open_orders(OrderSide.SELL) if o.price == 110.0)

        await stack.manager.apply_order_update(stack.execution.end(first_sell.identifier, OrderStatus.CANCELED))

        assert stack.grid.grid_levels[110.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert sorted(o.price for o in stack.execution.open_orders(OrderSide.SELL)) == [110.0, 110.0, 120.0]

    async def test_an_ended_order_without_a_grid_level_is_settled_and_notified(self, stack):
        await stack.balances.reserve_funds_for_buy(95.0)
        loose = await stack.execution.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 95.0, "gloose")
        stack.book.add_order(loose)
        orders_before = len(stack.execution.orders)

        await stack.manager.apply_order_update(stack.execution.end(loose.identifier, OrderStatus.CANCELED))

        assert not stack.book.get_order(loose.identifier).is_open()
        assert len(stack.execution.orders) == orders_before
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_CANCELLED in _sent(stack)

    async def test_a_rejected_order_without_a_grid_level_reports_the_rejection(self, stack):
        await stack.balances.reserve_funds_for_buy(95.0)
        loose = await stack.execution.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 95.0, "gloose")
        stack.book.add_order(loose)
        orders_before = len(stack.execution.orders)

        await stack.manager.apply_order_update(stack.execution.end(loose.identifier, OrderStatus.REJECTED))

        assert not stack.book.get_order(loose.identifier).is_open()
        assert len(stack.execution.orders) == orders_before
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_CANCELLED in _sent(stack)
        assert NotificationType.ERROR_OCCURRED in _sent(stack)

    async def test_a_failed_replacement_releases_its_reservation(self, stack):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)
        stack.execution.fail_next_limit_order = OrderRejectedError("insufficient funds")

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, OrderStatus.CANCELED))

        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_a_post_only_refused_replacement_waits_in_the_journal_without_an_error(self, stack):
        buy = stack.execution.order_at(90.0, OrderSide.BUY)
        stack.execution.fail_next_limit_order = PostOnlyRejectedError("would cross")

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, OrderStatus.CANCELED))

        [entry] = stack.journal.limit_entries()
        assert (entry.level_price, entry.purpose) == (90.0, PlacementPurpose.REPLACEMENT)
        assert reserved_matches_open_orders(stack)
        assert not {NotificationType.ORDER_FAILED, NotificationType.ERROR_OCCURRED} & set(_sent(stack))


class TestBulkCancellation:
    async def test_a_bulk_cancel_releases_every_reservation_and_frees_the_levels(self, stack):
        fiat_total = stack.balances.get_adjusted_fiat_balance()

        assert await stack.manager.cancel_open_grid_orders() is True

        assert stack.book.get_open_orders() == []
        assert stack.balances.reserved_fiat == pytest.approx(0.0, abs=1e-6)
        assert stack.balances.reserved_crypto == pytest.approx(0.0, abs=1e-6)
        assert stack.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_total)
        assert all(level.state == level.home_state for level in stack.grid.grid_levels.values())

    async def test_a_partly_filled_order_cancelled_in_bulk_books_its_fill_once(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.fill(buy.identifier, buy.amount * 0.4)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        assert await stack.manager.cancel_open_grid_orders() is True
        await stack.manager.apply_order_update(await stack.execution.get_order(buy.identifier, "BTC/USDT"))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + buy.amount * 0.4 * 0.999)
        assert stack.balances.reserved_fiat == pytest.approx(0.0, abs=1e-6)

    async def test_an_unreadable_cancelled_order_releases_its_remainder_as_last_seen(self, stack):
        async def unreadable(_order_id, _pair):
            raise DataFetchError("exchange down")

        stack.execution.get_order = unreadable

        assert await stack.manager.cancel_open_grid_orders() is True

        assert stack.balances.reserved_fiat == pytest.approx(0.0, abs=1e-6)
        assert stack.balances.reserved_crypto == pytest.approx(0.0, abs=1e-6)

    async def test_a_failed_cancellation_stops_the_bulk_and_keeps_the_rest_open(self, stack):
        real_cancel = stack.execution.cancel_order
        attempts = []

        async def second_fails(order_id, pair):
            attempts.append(order_id)
            return False if len(attempts) == 2 else await real_cancel(order_id, pair)

        stack.execution.cancel_order = second_fails

        assert await stack.manager.cancel_open_grid_orders() is False

        assert len(stack.book.get_open_orders()) == 3
        assert reserved_matches_open_orders(stack)


class TestPlacementEntryState:
    async def test_an_order_filled_at_creation_rests_until_the_tracker_reports_it(self):
        stack = build_stack()
        stack.execution.fill_limit_orders_on_placement = True
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        stack.execution.fill_limit_orders_on_placement = False
        placed = next(o for o in stack.execution.orders.values() if o.price == 100.0)
        local = stack.book.get_order(placed.identifier)
        assert (local.is_open(), local.filled, local.remaining) == (True, 0.0, placed.amount)
        assert reserved_matches_open_orders(stack)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.apply_order_update(await stack.execution.get_order(placed.identifier, "BTC/USDT"))

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + placed.amount * 0.999)
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(placed.amount * 0.999)


class TestClientOrderIds:
    async def test_every_grid_order_is_sent_with_a_client_order_id_of_this_config(self):
        stack = build_stack(config_hash="3f2a9c1d8e7b6a5f")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(stack.execution.client_ids) == 4
        assert all(re.fullmatch(r"g3f2a9c1d[0-9a-f]{16}", cid) for cid in stack.execution.client_ids)

    async def test_a_paired_order_gets_a_client_order_id_of_its_own(self, stack):
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        assert sell.identifier in stack.execution.client_ids.values()
        assert len(stack.execution.client_ids) == 5

    async def test_a_grid_order_that_was_not_placed_waits_in_the_journal(self):
        stack = build_stack()
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange unavailable")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(stack.execution.open_orders()) == 3
        assert len(stack.journal) == 1
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_PENDING in _sent(stack)

    async def test_an_initial_purchase_the_exchange_refuses_is_reported(self):
        stack = build_stack(crypto=0.0)
        stack.execution.market_order_failures = [OrderRejectedError("insufficient funds")]
        balances_before = (stack.balances.balance, stack.balances.crypto_balance)

        await stack.manager.perform_initial_purchase(ARMING_PRICE)

        assert (stack.balances.balance, stack.balances.crypto_balance) == balances_before
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_a_liquidation_the_exchange_refuses_is_reported(self, stack):
        stack.execution.market_order_failures = [OrderRejectedError("insufficient funds")]
        stack.manager.interrupt_liquidation()

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert stack.balances.crypto_balance == pytest.approx(20.0)
        assert NotificationType.ERROR_OCCURRED in _sent(stack)


class TestNotificationsAfterTheLock:
    @staticmethod
    def _record_sends(stack, fail_on=None):
        sent = []

        async def record(content, **_kwargs):
            sent.append((content, stack.manager._lock.locked()))
            if content is fail_on:
                raise RuntimeError("apprise down")

        stack.notifications.async_send_notification.side_effect = record
        return sent

    async def test_a_fill_notifies_after_the_lock_and_before_its_paired_order(self, stack):
        sent = self._record_sends(stack)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert sent == [(NotificationType.ORDER_FILLED, False), (NotificationType.ORDER_PLACED, False)]

    async def test_an_ended_order_notifies_after_the_lock(self, stack):
        sent = self._record_sends(stack)
        buy = stack.execution.order_at(90.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.end(buy.identifier, OrderStatus.CANCELED))

        assert sent == [(NotificationType.ORDER_CANCELLED, False)]

    async def test_a_failed_grid_order_notifies_after_the_lock(self):
        stack = build_stack()
        sent = self._record_sends(stack)
        stack.execution.fail_next_limit_order = OrderRejectedError("network down")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert (NotificationType.ORDER_FAILED, False) in sent
        assert all(not locked for _, locked in sent)

    async def test_a_notification_that_fails_does_not_stop_the_next(self, stack):
        sent = self._record_sends(stack, fail_on=NotificationType.ORDER_FILLED)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert [content for content, _ in sent] == [NotificationType.ORDER_FILLED, NotificationType.ORDER_PLACED]

    async def test_what_was_queued_before_an_error_still_goes_out(self, stack):
        sent = self._record_sends(stack)
        stack.book.get_grid_level_for_order = Mock(side_effect=RuntimeError("level broken"))
        buy = stack.execution.order_at(100.0, OrderSide.BUY)

        with pytest.raises(RuntimeError, match="level broken"):
            await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))

        assert sent == [(NotificationType.ORDER_FILLED, False)]
