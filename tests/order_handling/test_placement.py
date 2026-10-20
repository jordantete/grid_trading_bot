import asyncio
from decimal import Decimal
import logging
import re
from unittest.mock import AsyncMock

import pytest
from tests.fakes.recording_store import RecordingStore
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling import order_manager as order_manager_module
from grid_trading_bot.core.order_handling.exceptions import OrderExecutionFailedError
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.order_handling.order_manager import OrderManager
from grid_trading_bot.core.order_handling.placement_journal import PlacementPurpose
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    PostOnlyRejectedError,
)
from grid_trading_bot.core.services.market_constraints import MarketConstraints

# Arming at 115 places buys on 80, 90 and 100 and a sell on 120, in that order; 110 stays free.


@pytest.fixture
async def armed():
    stack = build_stack(store=RecordingStore())
    await stack.manager.initialize_grid_orders(ARMING_PRICE)
    return stack


def _sent(stack):
    return [call.args[0] for call in stack.notifications.async_send_notification.await_args_list]


def _errors(caplog):
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


class TestSubmitLimit:
    async def test_every_order_carries_a_client_id_drawn_for_it(self, armed):
        client_ids = list(armed.execution.client_ids)

        assert len(client_ids) == 4
        assert len(set(client_ids)) == 4
        assert all(re.fullmatch(r"g3f2a9c1d[0-9a-f]{16}", client_id) for client_id in client_ids)
        assert {o.identifier for o in armed.book.get_open_orders()} == set(armed.execution.client_ids.values())
        assert len(armed.journal) == 0
        assert reserved_matches_open_orders(armed)

    async def test_each_entry_is_saved_as_sent_before_its_order_reaches_the_exchange(self):
        store = RecordingStore()
        stack = build_stack(store=store)
        seen = []
        real_save_pending = store.save_pending

        async def save_pending(entry):
            seen.append((entry.sent, entry.client_order_id in stack.execution.client_ids))
            return await real_save_pending(entry)

        store.save_pending = save_pending

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert seen == [(True, False)] * 4

    async def test_a_level_still_holding_an_order_is_skipped(self, armed):
        await armed.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(armed.execution.open_orders()) == 4
        assert reserved_matches_open_orders(armed)

    async def test_reservations_follow_the_rounded_orders(self):
        stack = build_stack()
        stack.execution.amount_decimals = 3

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert [o.amount for o in stack.execution.open_orders()] == [8.695] * 4
        assert stack.balances.reserved_fiat == pytest.approx(8.695 * (80.0 + 90.0 + 100.0))
        assert stack.balances.reserved_crypto == pytest.approx(8.695)
        assert reserved_matches_open_orders(stack)

    async def test_a_transient_failure_keeps_the_entry_its_reservation_and_its_level(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        [entry] = stack.journal.limit_entries()
        assert (entry.side, entry.level_price, entry.purpose, entry.sent) == (
            OrderSide.BUY,
            80.0,
            PlacementPurpose.GRID,
            False,
        )
        assert sorted(o.price for o in stack.execution.open_orders(OrderSide.BUY)) == [90.0, 100.0]
        assert stack.grid.grid_levels[80.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(stack)
        assert _sent(stack).count(NotificationType.ORDER_PENDING) == 1

    async def test_an_unknown_outcome_keeps_the_entry_marked_sent(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.lose_next_limit_response = OrderOutcomeUnknownError("timeout")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        [entry] = stack.journal.limit_entries()
        held = stack.execution.client_ids[entry.client_order_id]
        assert entry.sent is True
        assert stack.execution.orders[held].status == OrderStatus.OPEN
        assert stack.book.get_order(held) is None
        assert reserved_matches_open_orders(stack)

    async def test_a_plain_exchange_error_keeps_the_entry_marked_sent(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = DataFetchError("malformed answer")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        [entry] = stack.journal.limit_entries()
        assert entry.sent is True
        assert reserved_matches_open_orders(stack)

    async def test_a_post_only_refusal_keeps_the_entry_without_an_alert(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = PostOnlyRejectedError("would cross")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        [entry] = stack.journal.limit_entries()
        assert (entry.level_price, entry.sent) == (80.0, False)
        assert len(stack.execution.open_orders()) == 3
        assert stack.grid.grid_levels[80.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(stack)
        assert not {
            NotificationType.ORDER_PENDING,
            NotificationType.ORDER_FAILED,
            NotificationType.ERROR_OCCURRED,
        } & set(_sent(stack))

    async def test_an_exchange_refusal_drops_the_entry_and_frees_the_level(self, caplog):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderRejectedError("insufficient balance")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(stack.journal) == 0
        assert sorted(o.price for o in stack.execution.open_orders(OrderSide.BUY)) == [90.0, 100.0]
        assert stack.grid.grid_levels[80.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert _errors(caplog) == []

    async def test_an_execution_failure_drops_the_entry_and_reports_a_failed_order(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderExecutionFailedError(
            "exchange refused", OrderSide.BUY, OrderType.LIMIT, "BTC/USDT", 1.0, 80.0
        )

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(stack.journal) == 0
        assert stack.grid.grid_levels[80.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_an_unexpected_error_drops_the_entry_and_alerts(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = RuntimeError("boom")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(stack.journal) == 0
        assert stack.grid.grid_levels[80.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ERROR_OCCURRED in _sent(stack)

    async def test_a_local_refusal_creates_nothing_and_logs_no_error(self, caplog):
        stack = build_stack(fiat=10.0, store=RecordingStore())

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert stack.execution.open_orders(OrderSide.BUY) == []
        assert [o.price for o in stack.execution.open_orders(OrderSide.SELL)] == [120.0]
        assert len(stack.journal) == 0
        assert all(stack.grid.grid_levels[price].state == GridCycleState.READY_TO_BUY for price in (80.0, 90.0, 100.0))
        assert reserved_matches_open_orders(stack)
        assert _errors(caplog) == []

    async def test_a_level_under_the_exchange_minimum_is_not_placed(self):
        stack = build_stack(store=RecordingStore())
        stack.manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=10_000.0))

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert stack.execution.orders == {}
        assert len(stack.journal) == 0
        assert reserved_matches_open_orders(stack)

    async def test_a_failed_save_sends_nothing_and_keeps_every_entry_unsent(self):
        store = RecordingStore()
        store.fail_pending = True
        stack = build_stack(store=store)

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert stack.execution.orders == {}
        assert [entry.sent for entry in stack.journal.limit_entries()] == [False] * 4
        assert all(
            stack.grid.grid_levels[price].state
            in (GridCycleState.WAITING_FOR_BUY_FILL, GridCycleState.WAITING_FOR_SELL_FILL)
            for price in (80.0, 90.0, 100.0, 120.0)
        )
        assert reserved_matches_open_orders(stack)

    async def test_nothing_is_placed_once_the_positions_are_closed(self):
        stack = build_stack(store=RecordingStore())
        stack.manager._positions_closed = True

        placed = await stack.manager._submit_limit(
            OrderSide.BUY, stack.grid.grid_levels[80.0], 1.0, PlacementPurpose.GRID
        )

        assert placed is None
        assert len(stack.journal) == 0
        assert stack.execution.orders == {}

    async def test_a_close_landing_mid_round_stops_the_grid_placement(self):
        stack = build_stack(store=RecordingStore())
        real_execute = stack.execution.execute_limit_order

        async def place_then_close(*args):
            order = await real_execute(*args)
            stack.manager._positions_closed = True
            return order

        stack.execution.execute_limit_order = place_then_close

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert len(stack.execution.open_orders()) == 1
        assert len(stack.journal) == 0

    async def test_an_order_that_cannot_be_adopted_keeps_its_sent_entry_and_reservation(self, monkeypatch):
        stack = build_stack(store=RecordingStore())
        real_adopt = order_manager_module.adopt_order
        calls = []

        def fail_first(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("book broken")
            return real_adopt(*args)

        monkeypatch.setattr(order_manager_module, "adopt_order", fail_first)

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        [entry] = stack.journal.limit_entries()
        assert (entry.level_price, entry.sent) == (80.0, True)
        assert stack.book.get_order(stack.execution.client_ids[entry.client_order_id]) is None
        assert len(stack.book.get_open_orders()) == 3
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ERROR_OCCURRED in _sent(stack)


class TestPairedOrders:
    async def test_a_filled_buy_places_its_sell_on_the_net_crypto_and_pairs_the_levels(self, armed):
        buy = armed.execution.order_at(100.0, OrderSide.BUY)

        await armed.manager.apply_order_update(armed.execution.fill(buy.identifier))

        sell = armed.execution.order_at(110.0, OrderSide.SELL)
        assert sell.amount == pytest.approx(buy.amount * 0.999)
        assert armed.grid.grid_levels[100.0].paired_sell_level is armed.grid.grid_levels[110.0]
        assert armed.grid.grid_levels[110.0].paired_buy_level is armed.grid.grid_levels[100.0]
        assert NotificationType.ORDER_PLACED in _sent(armed)
        assert len(armed.journal) == 0
        assert reserved_matches_open_orders(armed)

    async def test_a_buy_filled_below_its_limit_price_leaves_no_fiat_reserved_for_it(self, armed):
        buy = armed.execution.order_at(100.0, OrderSide.BUY)
        fiat_total = armed.balances.get_adjusted_fiat_balance()

        update = armed.execution.fill(buy.identifier)
        update.average = 95.0
        await armed.manager.apply_order_update(update)

        assert reserved_matches_open_orders(armed)
        assert armed.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_total - buy.amount * 95.0)

    async def test_the_sell_ratio_scales_the_paired_sell(self, armed):
        armed.grid.sell_ratio = 0.5
        buy = armed.execution.order_at(100.0, OrderSide.BUY)

        await armed.manager.apply_order_update(armed.execution.fill(buy.identifier))

        assert armed.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount * 0.999 * 0.5)

    async def test_the_buy_ratio_scales_the_rebuy(self, armed):
        await armed.manager.apply_order_update(
            armed.execution.fill(armed.execution.order_at(100.0, OrderSide.BUY).identifier)
        )
        armed.grid.buy_ratio = 0.5
        sell = armed.execution.order_at(110.0, OrderSide.SELL)

        await armed.manager.apply_order_update(armed.execution.fill(sell.identifier))

        assert armed.execution.order_at(100.0, OrderSide.BUY).amount == pytest.approx(sell.amount * 0.5)
        assert reserved_matches_open_orders(armed)

    async def test_a_sell_fill_without_a_free_buy_level_places_nothing(self, armed):
        sell = armed.execution.order_at(120.0, OrderSide.SELL)

        await armed.manager.apply_order_update(armed.execution.fill(sell.identifier))

        assert len(armed.execution.open_orders(OrderSide.BUY)) == 3
        assert armed.execution.open_orders(OrderSide.SELL) == []
        assert reserved_matches_open_orders(armed)

    async def test_a_paired_sell_stacks_on_the_lowest_level_when_every_sell_level_is_busy(self, armed):
        await armed.manager.apply_order_update(
            armed.execution.fill(armed.execution.order_at(100.0, OrderSide.BUY).identifier)
        )

        await armed.manager.apply_order_update(
            armed.execution.fill(armed.execution.order_at(90.0, OrderSide.BUY).identifier)
        )

        assert sorted(o.price for o in armed.execution.open_orders(OrderSide.SELL)) == [110.0, 110.0, 120.0]
        assert reserved_matches_open_orders(armed)

    async def test_a_paired_sell_under_the_minimum_after_the_fee_is_refused_without_an_entry(self, armed):
        buy = armed.execution.order_at(100.0, OrderSide.BUY)
        armed.manager.set_market_constraints(MarketConstraints(min_amount=buy.amount * 0.9995, min_cost=None))
        crypto_before = armed.balances.crypto_balance
        orders_before = len(armed.execution.orders)

        await armed.manager.apply_order_update(armed.execution.fill(buy.identifier))

        assert len(armed.execution.orders) == orders_before
        assert len(armed.journal) == 0
        assert armed.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert armed.balances.crypto_balance == pytest.approx(crypto_before + buy.amount * 0.999)
        assert reserved_matches_open_orders(armed)
        assert _sent(armed).count(NotificationType.ORDER_FAILED) == 1

    async def test_a_level_held_by_an_entry_is_not_targeted_twice(self, armed):
        await armed.manager.apply_order_update(
            armed.execution.fill(armed.execution.order_at(120.0, OrderSide.SELL).identifier)
        )
        armed.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")
        await armed.manager.apply_order_update(
            armed.execution.fill(armed.execution.order_at(100.0, OrderSide.BUY).identifier)
        )
        buy_90 = armed.execution.order_at(90.0, OrderSide.BUY)

        await armed.manager.apply_order_update(armed.execution.fill(buy_90.identifier))

        [entry] = armed.journal.limit_entries()
        assert (entry.side, entry.level_price, entry.purpose) == (OrderSide.SELL, 110.0, PlacementPurpose.PAIRED)
        sells = armed.execution.open_orders(OrderSide.SELL)
        assert [o.price for o in sells] == [120.0]
        assert sells[0].amount == pytest.approx(buy_90.amount * 0.999)
        assert reserved_matches_open_orders(armed)

    async def test_a_paired_sell_the_exchange_refuses_is_dropped_with_one_alert(self, armed, caplog):
        armed.execution.fail_next_limit_order = OrderRejectedError("insufficient balance")
        buy = armed.execution.order_at(100.0, OrderSide.BUY)

        await armed.manager.apply_order_update(armed.execution.fill(buy.identifier))

        assert [o.price for o in armed.execution.open_orders(OrderSide.SELL)] == [120.0]
        assert armed.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert len(armed.journal) == 0
        assert reserved_matches_open_orders(armed)
        assert _sent(armed).count(NotificationType.ORDER_FAILED) == 1
        assert _errors(caplog) == []

    async def test_a_paired_sell_waits_in_the_journal_while_the_exchange_is_down(self, armed):
        armed.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")
        buy = armed.execution.order_at(100.0, OrderSide.BUY)

        await armed.manager.apply_order_update(armed.execution.fill(buy.identifier))

        [entry] = armed.journal.limit_entries()
        assert (entry.level_price, entry.source_level_price) == (110.0, 100.0)
        assert entry.source_order_id == buy.identifier
        assert entry.quantity == pytest.approx(buy.amount * 0.999)
        assert armed.grid.grid_levels[110.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert reserved_matches_open_orders(armed)
        assert _sent(armed).count(NotificationType.ORDER_PENDING) == 1


class TestReplacements:
    async def test_a_replacement_the_exchange_cannot_take_yet_waits_in_the_journal(self, armed):
        buy = armed.execution.order_at(90.0, OrderSide.BUY)
        armed.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")

        await armed.manager.apply_order_update(armed.execution.end(buy.identifier, OrderStatus.CANCELED))

        [entry] = armed.journal.limit_entries()
        assert (entry.level_price, entry.purpose, entry.sent) == (90.0, PlacementPurpose.REPLACEMENT, False)
        assert entry.source_order_id == buy.identifier
        assert entry.quantity == pytest.approx(buy.amount)
        assert armed.grid.grid_levels[90.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(armed)


class TestRecoveredFills:
    @staticmethod
    async def _recovered_buy(stack, price):
        """A buy that filled while the bot was down, as recovery leaves it: booked, out of the open orders, its
        level released."""
        order = stack.execution.order_at(price, OrderSide.BUY)
        local = stack.book.get_order(order.identifier)
        newly_filled = local.apply_update(stack.execution.fill(order.identifier))
        stack.book.remove_open_order(local)
        await stack.balances.record_execution(local, newly_filled)
        level = stack.grid.grid_levels[price]
        stack.grid.complete_order(level, OrderSide.BUY, False)
        return local, level

    async def test_each_recovered_fill_gets_its_paired_order(self, armed):
        fills = [await self._recovered_buy(armed, 100.0), await self._recovered_buy(armed, 90.0)]

        await armed.manager.place_paired_orders_for_recovered_fills(fills)

        assert sorted(o.price for o in armed.execution.open_orders(OrderSide.SELL)) == [110.0, 110.0, 120.0]
        assert armed.grid.grid_levels[100.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(armed)

    async def test_one_failing_recovered_pair_does_not_stop_the_next(self, armed):
        fills = [await self._recovered_buy(armed, 100.0), await self._recovered_buy(armed, 90.0)]
        armed.execution.fail_next_limit_order = RuntimeError("boom")

        await armed.manager.place_paired_orders_for_recovered_fills(fills)

        assert sorted(o.price for o in armed.execution.open_orders(OrderSide.SELL)) == [110.0, 120.0]
        assert len(armed.journal) == 0
        assert reserved_matches_open_orders(armed)
        assert NotificationType.ERROR_OCCURRED in _sent(armed)

    async def test_recovered_fills_place_nothing_once_the_positions_are_closed(self, armed):
        fills = [await self._recovered_buy(armed, 100.0)]
        armed.manager._positions_closed = True

        await armed.manager.place_paired_orders_for_recovered_fills(fills)

        assert [o.price for o in armed.execution.open_orders(OrderSide.SELL)] == [120.0]
        assert len(armed.journal) == 0


def _order_manager_warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "OrderManager" and record.levelno == logging.WARNING
    ]


class TestGridRoundSummary:
    async def test_a_round_with_unfunded_levels_logs_one_warning_and_sends_one_alert(self, caplog):
        caplog.set_level(logging.INFO)
        stack = build_stack(fiat=10.0, store=RecordingStore())

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert _order_manager_warnings(caplog) == [
            "Grid placement left 3 buy and 0 sell levels unfunded: [80.0, 90.0, 100.0]"
        ]
        assert _sent(stack).count(NotificationType.ORDER_FAILED) == 1
        assert _errors(caplog) == []

    async def test_an_exchange_refusal_counts_in_the_round_summary(self, caplog):
        caplog.set_level(logging.INFO)
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderRejectedError("insufficient balance")

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert _order_manager_warnings(caplog) == ["Grid placement left 1 buy and 0 sell levels unfunded: [80.0]"]
        assert _sent(stack).count(NotificationType.ORDER_FAILED) == 1

    async def test_a_fully_funded_round_reports_nothing(self, caplog):
        caplog.set_level(logging.INFO)
        stack = build_stack(store=RecordingStore())

        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        assert not any("unfunded" in message for message in _order_manager_warnings(caplog))
        assert NotificationType.ORDER_FAILED not in _sent(stack)

    async def test_a_regrid_round_reports_its_own_unfunded_levels(self, caplog):
        caplog.set_level(logging.INFO)
        stack = build_stack(fiat=10.0, store=RecordingStore())
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        caplog.clear()

        assert await stack.manager.regrid(ARMING_PRICE, lambda: None) is True

        assert _order_manager_warnings(caplog) == [
            "Grid placement left 3 buy and 0 sell levels unfunded: [80.0, 90.0, 100.0]"
        ]


async def _arm_with_one_lost_placement(error):
    """Arms the grid while the 80 buy's answer is lost after the exchange took it; returns the stack, the entry and
    the exchange id of the order the exchange holds for it."""
    stack = build_stack(store=RecordingStore())
    stack.execution.lose_next_limit_response = error
    await stack.manager.initialize_grid_orders(ARMING_PRICE)
    [entry] = stack.journal.limit_entries()
    return stack, entry, stack.execution.client_ids[entry.client_order_id]


class TestRetries:
    async def test_an_unsent_entry_is_sent_at_the_next_retry(self, caplog):
        caplog.set_level(logging.INFO)
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        await stack.manager.retry_pending_placements()

        assert len(stack.journal) == 0
        placed = stack.execution.order_at(80.0, OrderSide.BUY)
        assert stack.book.get_order(placed.identifier).is_open()
        assert stack.grid.grid_levels[80.0].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(stack)
        assert any("placed after 2 attempts" in record.getMessage() for record in caplog.records)

    async def test_a_sent_entry_the_exchange_holds_is_adopted_without_a_second_order(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        orders_before = len(stack.execution.orders)

        await stack.manager.retry_pending_placements()

        assert len(stack.execution.orders) == orders_before
        assert stack.book.get_order(held).is_open()
        assert len(stack.journal) == 0
        assert reserved_matches_open_orders(stack)

    async def test_a_sent_entry_the_exchange_filled_is_booked_and_paired(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        amount = stack.execution.orders[held].amount
        stack.execution.fill(held)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.retry_pending_placements()

        assert stack.book.get_order(held).is_filled()
        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + amount * 0.999)
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(amount * 0.999)
        assert len(stack.journal) == 0
        assert reserved_matches_open_orders(stack)

    async def test_a_sent_entry_found_cancelled_is_placed_again_under_a_new_id(self):
        stack, entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        stack.execution.end(held, OrderStatus.CANCELED)

        await stack.manager.retry_pending_placements()

        replacement = stack.execution.order_at(80.0, OrderSide.BUY)
        assert replacement.identifier != held
        [new_client_id] = [cid for cid, oid in stack.execution.client_ids.items() if oid == replacement.identifier]
        assert new_client_id != entry.client_order_id
        assert len(stack.journal) == 0
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ORDER_CANCELLED in _sent(stack)

    async def test_a_sent_entry_found_rejected_is_dropped_with_an_alert(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        stack.execution.end(held, OrderStatus.REJECTED)

        await stack.manager.retry_pending_placements()

        assert sorted(o.price for o in stack.execution.open_orders(OrderSide.BUY)) == [90.0, 100.0]
        assert stack.grid.grid_levels[80.0].state == GridCycleState.READY_TO_BUY
        assert len(stack.journal) == 0
        assert reserved_matches_open_orders(stack)
        assert NotificationType.ERROR_OCCURRED in _sent(stack)

    async def test_a_sent_entry_the_exchange_never_received_is_sent_again_under_its_id(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderOutcomeUnknownError("timeout")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        [entry] = stack.journal.limit_entries()
        assert entry.sent
        assert entry.client_order_id not in stack.execution.client_ids

        await stack.manager.retry_pending_placements()

        placed = stack.execution.client_ids[entry.client_order_id]
        assert stack.book.get_order(placed).is_open()
        assert len(stack.journal) == 0
        assert reserved_matches_open_orders(stack)

    async def test_a_failed_lookup_keeps_the_entry_for_the_next_poll(self):
        stack, entry, _held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        orders_before = len(stack.execution.orders)

        async def unreachable(_client_order_id, _pair):
            raise DataFetchError("exchange down")

        stack.execution.find_order = unreachable

        await stack.manager.retry_pending_placements()

        assert stack.journal.limit_entries() == [entry]
        assert entry.sent
        assert len(stack.execution.orders) == orders_before
        assert reserved_matches_open_orders(stack)

    async def test_a_refusal_at_retry_drops_the_entry_with_one_alert(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        stack.execution.fail_next_limit_order = OrderRejectedError("insufficient balance")

        await stack.manager.retry_pending_placements()

        assert len(stack.journal) == 0
        assert stack.grid.grid_levels[80.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)
        assert _sent(stack).count(NotificationType.ORDER_FAILED) == 1

    async def test_entries_are_dropped_once_the_positions_are_closed(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        stack.manager._positions_closed = True
        orders_before = len(stack.execution.orders)

        await stack.manager.retry_pending_placements()

        assert len(stack.journal) == 0
        assert len(stack.execution.orders) == orders_before
        assert stack.grid.grid_levels[80.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(stack)

    async def test_a_sent_entry_is_kept_once_the_positions_are_closed(self):
        stack, entry, _held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        stack.manager._positions_closed = True
        calls = list(stack.execution.calls)

        await stack.manager.retry_pending_placements()

        assert stack.journal.limit_entries() == [entry]
        assert entry.sent
        assert stack.execution.calls == calls
        assert reserved_matches_open_orders(stack)

    async def test_a_post_only_entry_is_retried_until_the_exchange_takes_it(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = PostOnlyRejectedError("would cross")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        await stack.manager.retry_pending_placements()

        assert stack.book.get_order(stack.execution.order_at(80.0, OrderSide.BUY).identifier).is_open()
        assert len(stack.journal) == 0

    async def test_a_retry_saves_the_state_once(self):
        store = RecordingStore()
        stack = build_stack(store=store)
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange down")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        saves_before = len(store.saves)

        await stack.manager.retry_pending_placements()

        assert len(store.saves) == saves_before + 1

    async def test_a_retry_with_an_empty_journal_does_nothing(self, armed):
        saves_before = len(armed.store.saves)
        calls_before = list(armed.execution.calls)

        await armed.manager.retry_pending_placements()

        assert len(armed.store.saves) == saves_before
        assert armed.execution.calls == calls_before

    async def test_an_entry_that_fails_does_not_stop_the_retry_of_the_next(self, monkeypatch, caplog):
        store = RecordingStore()
        stack = build_stack(store=store)
        real_execute = stack.execution.execute_limit_order
        placements = []

        async def first_two_not_placed(*args):
            placements.append(args)
            if len(placements) <= 2:
                raise OrderNotPlacedError("exchange down")
            return await real_execute(*args)

        stack.execution.execute_limit_order = first_two_not_placed
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        first, second = stack.journal.limit_entries()
        real_adopt = order_manager_module.adopt_order
        adoptions = []

        def fail_first(*args):
            adoptions.append(args)
            if len(adoptions) == 1:
                raise RuntimeError("book broken")
            return real_adopt(*args)

        monkeypatch.setattr(order_manager_module, "adopt_order", fail_first)
        saves_before = len(store.saves)

        await stack.manager.retry_pending_placements()

        assert stack.journal.limit_entries() == [first]
        assert first.sent
        assert stack.book.get_order(stack.execution.client_ids[second.client_order_id]).is_open()
        assert stack.grid.grid_levels[second.level_price].state == GridCycleState.WAITING_FOR_BUY_FILL
        assert reserved_matches_open_orders(stack)
        assert _sent(stack).count(NotificationType.ERROR_OCCURRED) == 1
        assert any("book broken" in message for message in _errors(caplog))
        assert len(store.saves) == saves_before + 1


class TestBulkCancelResolvesSentEntries:
    async def test_a_regrid_resolves_a_sent_entry_before_rebuilding(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        at_rebuild = []

        rebuilt = await stack.manager.regrid(
            ARMING_PRICE,
            lambda: at_rebuild.append((len(stack.journal), [o.identifier for o in stack.execution.open_orders()])),
        )

        assert rebuilt is True
        assert at_rebuild == [(0, [])]
        assert stack.execution.orders[held].status == OrderStatus.CANCELED
        assert len(stack.execution.open_orders()) == 4
        assert reserved_matches_open_orders(stack)

    async def test_a_regrid_drops_a_sent_entry_the_exchange_never_received(self):
        stack = build_stack(store=RecordingStore())
        stack.execution.fail_next_limit_order = OrderOutcomeUnknownError("timeout")
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        at_rebuild = []

        await stack.manager.regrid(
            ARMING_PRICE,
            lambda: at_rebuild.append((len(stack.journal), len(stack.execution.open_orders()))),
        )

        assert at_rebuild == [(0, 0)]
        assert len(stack.execution.open_orders()) == 4
        assert reserved_matches_open_orders(stack)

    async def test_a_bulk_cancel_stops_when_a_sent_entry_cannot_be_looked_up(self):
        stack, entry, _held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))

        async def unreachable(_client_order_id, _pair):
            raise DataFetchError("exchange down")

        stack.execution.find_order = unreachable

        assert await stack.manager.cancel_open_grid_orders() is False

        assert stack.journal.limit_entries() == [entry]
        assert len(stack.book.get_open_orders()) == 3
        assert reserved_matches_open_orders(stack)

    async def test_closing_the_positions_books_a_sent_entry_the_exchange_filled(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        amount = stack.execution.orders[held].amount
        stack.execution.fill(held)
        crypto_before = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.close_positions(liquidate=False)

        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_before + amount * 0.999)
        assert stack.execution.open_orders() == []
        assert len(stack.journal) == 0
        assert stack.balances.reserved_fiat == pytest.approx(0.0, abs=1e-6)
        assert stack.balances.reserved_crypto == pytest.approx(0.0, abs=1e-6)

    @staticmethod
    def _watch_regrid(stack):
        """Records the journal size, the open and total exchange orders at rebuild, and the sent flag of each drop."""
        dropped_sent = []
        real_drop = stack.manager._drop_entry

        async def drop(entry):
            dropped_sent.append(entry.sent)
            await real_drop(entry)

        stack.manager._drop_entry = drop
        at_rebuild = []

        def rebuild():
            at_rebuild.append((len(stack.journal), len(stack.execution.open_orders()), len(stack.execution.orders)))

        return rebuild, at_rebuild, dropped_sent

    async def test_a_regrid_books_a_sent_entry_found_filled_without_placing_its_paired_order(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        amount = stack.execution.orders[held].amount
        stack.execution.fill(held)
        crypto_before = stack.balances.get_adjusted_crypto_balance()
        orders_before = len(stack.execution.orders)
        rebuild, at_rebuild, dropped_sent = self._watch_regrid(stack)
        crypto_at_rebuild = []

        assert await stack.manager.regrid(
            ARMING_PRICE, lambda: (rebuild(), crypto_at_rebuild.append(stack.balances.get_adjusted_crypto_balance()))
        )

        assert at_rebuild == [(0, 0, orders_before)]
        assert crypto_at_rebuild == [pytest.approx(crypto_before + amount * 0.999)]
        assert True not in dropped_sent
        assert NotificationType.ORDER_FILLED in _sent(stack)
        assert reserved_matches_open_orders(stack)

    async def test_a_regrid_settles_a_sent_entry_found_cancelled_without_a_replacement(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        stack.execution.end(held, OrderStatus.CANCELED)
        orders_before = len(stack.execution.orders)
        rebuild, at_rebuild, dropped_sent = self._watch_regrid(stack)

        assert await stack.manager.regrid(ARMING_PRICE, rebuild)

        assert at_rebuild == [(0, 0, orders_before)]
        assert True not in dropped_sent
        assert NotificationType.ORDER_CANCELLED in _sent(stack)
        assert reserved_matches_open_orders(stack)

    async def test_a_bulk_cancel_transitions_the_level_of_a_sent_entry_found_filled(self):
        stack, entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        stack.execution.fill(held)
        orders_before = len(stack.execution.orders)

        assert await stack.manager.cancel_open_grid_orders() is True

        assert len(stack.execution.orders) == orders_before
        assert stack.grid.grid_levels[entry.level_price].state == GridCycleState.READY_TO_BUY
        assert len(stack.journal) == 0
        assert stack.balances.reserved_fiat == pytest.approx(0.0, abs=1e-6)
        assert stack.balances.reserved_crypto == pytest.approx(0.0, abs=1e-6)

    async def test_the_pipeline_places_again_once_a_bulk_cancel_stopped_on_a_failed_lookup(self):
        stack, _entry, held = await _arm_with_one_lost_placement(OrderOutcomeUnknownError("timeout"))
        real_find = stack.execution.find_order
        stack.execution.fill(held)

        async def unreachable(_client_order_id, _pair):
            raise DataFetchError("exchange down")

        stack.execution.find_order = unreachable
        assert await stack.manager.cancel_open_grid_orders() is False
        stack.execution.find_order = real_find

        await stack.manager.retry_pending_placements()

        assert stack.book.get_order(stack.execution.order_at(110.0, OrderSide.SELL).identifier).is_open()
        assert len(stack.journal) == 0


class TestInitialPurchase:
    @staticmethod
    def _market_orders(stack):
        return [o for o in stack.execution.orders.values() if o.order_type == OrderType.MARKET]

    @staticmethod
    def _quantity(stack):
        return stack.grid.get_initial_order_quantity(2700.0, 0.0, ARMING_PRICE)

    async def test_the_purchase_buys_once_and_books_the_net_crypto(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        quantity = self._quantity(stack)

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        [order] = self._market_orders(stack)
        assert order.amount == pytest.approx(quantity)
        assert stack.balances.crypto_balance == pytest.approx(quantity * 0.999)
        assert stack.balances.balance == pytest.approx(2700.0 - quantity * ARMING_PRICE)
        assert stack.manager.arming.initial_purchase_done is True
        assert len(stack.journal) == 0
        assert stack.book.get_order(order.identifier) is not None

    async def test_a_purchase_with_nothing_to_buy_is_done_without_an_order(self):
        stack = build_stack(store=RecordingStore())

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert self._market_orders(stack) == []
        assert stack.manager.arming.initial_purchase_done is True

    async def test_an_unreachable_exchange_leaves_the_purchase_to_do(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange down")]

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        assert stack.manager.arming.initial_purchase_done is False
        assert self._market_orders(stack) == []
        assert stack.balances.crypto_balance == 0.0
        [entry] = stack.journal.entries()
        assert (entry.purpose, entry.order_type, entry.sent) == (
            PlacementPurpose.INITIAL_PURCHASE,
            OrderType.MARKET,
            False,
        )
        assert _sent(stack).count(NotificationType.ORDER_PENDING) == 1

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert len(self._market_orders(stack)) == 1
        assert len(stack.journal) == 0
        assert stack.manager.arming.initial_purchase_done is True
        assert _sent(stack).count(NotificationType.ORDER_PENDING) == 1

    async def test_an_unknown_outcome_is_looked_up_before_buying_again(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.execution.market_order_failures = [OrderOutcomeUnknownError("timeout")]
        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False
        [entry] = stack.journal.entries()
        assert entry.sent
        # The exchange had executed it under the entry's client id; only the answer was lost.
        await stack.execution.execute_market_order(
            OrderSide.BUY, "BTC/USDT", entry.quantity, ARMING_PRICE, entry.client_order_id
        )

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert len(self._market_orders(stack)) == 1
        assert stack.balances.crypto_balance == pytest.approx(entry.quantity * 0.999)
        assert len(stack.journal) == 0

    async def test_a_partial_leg_buys_the_rest_in_a_new_leg(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        quantity = self._quantity(stack)
        stack.execution.partial_market_fill = 0.5

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        first, second = self._market_orders(stack)
        assert first.filled == pytest.approx(quantity * 0.5)
        assert second.amount == pytest.approx(quantity * 0.5)
        assert stack.balances.crypto_balance == pytest.approx(quantity * 0.999)
        assert len(stack.journal) == 0

    async def test_legs_stop_after_three(self, caplog):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        quantity = self._quantity(stack)
        real_execute = stack.execution.execute_market_order

        async def half_fill(*args):
            stack.execution.partial_market_fill = 0.5
            return await real_execute(*args)

        stack.execution.execute_market_order = half_fill

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert len(self._market_orders(stack)) == 3
        assert stack.balances.crypto_balance == pytest.approx(quantity * (0.5 + 0.25 + 0.125) * 0.999)
        assert any(
            "stopped after 3 legs" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
        assert len(stack.journal) == 0

    async def test_an_exchange_refusal_marks_the_purchase_done_with_an_alert(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.execution.market_order_failures = [OrderRejectedError("insufficient balance")]

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert stack.balances.crypto_balance == 0.0
        assert len(stack.journal) == 0
        assert stack.manager.arming.initial_purchase_done is True
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_a_purchase_under_the_exchange_minimum_is_done_with_an_alert(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=100_000.0))

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert self._market_orders(stack) == []
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_no_purchase_once_the_positions_are_closed(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.manager._positions_closed = True

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert self._market_orders(stack) == []

    async def test_the_entry_is_saved_as_sent_before_the_market_order_goes_out(self):
        store = RecordingStore()
        stack = build_stack(crypto=0.0, store=store)
        seen = []
        real_save_pending = store.save_pending

        async def save_pending(entry):
            seen.append((entry.purpose, entry.sent, len(self._market_orders(stack))))
            return await real_save_pending(entry)

        store.save_pending = save_pending

        await stack.manager.perform_initial_purchase(ARMING_PRICE)

        assert seen == [(PlacementPurpose.INITIAL_PURCHASE, True, 0)]

    async def test_a_failed_save_sends_nothing_and_leaves_the_purchase_to_do(self):
        store = RecordingStore()
        store.fail_pending = True
        stack = build_stack(crypto=0.0, store=store)

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        assert self._market_orders(stack) == []
        assert stack.manager.arming.initial_purchase_done is False

    async def test_an_unexpected_error_keeps_the_purchase_to_do_and_alerts_once(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.execution.market_order_failures = [RuntimeError("boom"), RuntimeError("boom")]

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False
        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False
        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert len(self._market_orders(stack)) == 1
        assert _sent(stack).count(NotificationType.ERROR_OCCURRED) == 1

    @staticmethod
    async def _lose_the_answer(stack):
        """Sends the purchase with a lost answer, then lets the exchange execute it; returns the journal entry."""
        stack.execution.market_order_failures = [OrderOutcomeUnknownError("timeout")]
        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False
        [entry] = stack.journal.entries()
        await stack.execution.execute_market_order(
            OrderSide.BUY, "BTC/USDT", entry.quantity, ARMING_PRICE, entry.client_order_id
        )
        return entry

    async def test_a_failing_lookup_alerts_once(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.execution.market_order_failures = [OrderOutcomeUnknownError("timeout")]
        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        async def failing_lookup(client_order_id, pair):
            raise DataFetchError("exchange down")

        stack.execution.find_order = failing_lookup
        for _ in range(3):
            assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        assert _sent(stack).count(NotificationType.ORDER_PENDING) == 1
        assert stack.journal.entries()[0].sent

    async def test_an_unexpected_lookup_error_alerts_once(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        stack.execution.market_order_failures = [RuntimeError("boom")]
        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        async def failing_lookup(client_order_id, pair):
            raise RuntimeError("boom")

        stack.execution.find_order = failing_lookup
        for _ in range(3):
            assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        assert _sent(stack).count(NotificationType.ERROR_OCCURRED) == 1

    @pytest.mark.parametrize("filled_fraction", [0.5, 0.0])
    async def test_an_order_found_still_open_is_neither_booked_nor_bought_again(self, filled_fraction):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        entry = await self._lose_the_answer(stack)
        found = stack.execution.orders[stack.execution.client_ids[entry.client_order_id]]
        found.status = OrderStatus.OPEN
        found.filled = entry.quantity * filled_fraction
        found.remaining = entry.quantity - found.filled
        market_calls = stack.execution.calls.count("execute_market_order")

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        assert stack.execution.calls.count("execute_market_order") == market_calls
        assert stack.balances.crypto_balance == 0.0
        assert stack.manager.arming.initial_purchase_done is False
        [kept] = stack.journal.entries()
        assert kept.client_order_id == entry.client_order_id
        assert kept.sent

    async def test_an_order_found_closed_without_an_average_is_booked_at_its_price(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        entry = await self._lose_the_answer(stack)
        found = stack.execution.orders[stack.execution.client_ids[entry.client_order_id]]
        found.average = None

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert len(self._market_orders(stack)) == 1
        assert stack.balances.crypto_balance == pytest.approx(entry.quantity * 0.999)
        assert stack.balances.balance == pytest.approx(2700.0 - entry.quantity * found.price)
        assert len(stack.journal) == 0

    async def test_an_order_found_ended_with_nothing_filled_is_sent_again(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        entry = await self._lose_the_answer(stack)
        found = stack.execution.orders[stack.execution.client_ids[entry.client_order_id]]
        found.status, found.filled, found.remaining = OrderStatus.CANCELED, 0.0, entry.quantity
        old_client_id = entry.client_order_id
        seen = []
        real_execute = stack.execution.execute_market_order

        async def execute(side, pair, quantity, price, client_order_id):
            seen.append((client_order_id, tuple(e.client_order_id for e in stack.journal.entries())))
            return await real_execute(side, pair, quantity, price, client_order_id)

        stack.execution.execute_market_order = execute

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        [(new_client_id, journal_ids)] = seen
        assert new_client_id != old_client_id
        assert journal_ids == (new_client_id,)

        assert stack.balances.crypto_balance == pytest.approx(entry.quantity * 0.999)
        assert len(stack.journal) == 0

    @pytest.mark.parametrize("filled", [0.0, None])
    async def test_an_order_found_closed_without_a_fill_is_sent_again_under_a_new_client_id(self, filled):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        entry = await self._lose_the_answer(stack)
        found = stack.execution.orders[stack.execution.client_ids[entry.client_order_id]]
        found.status, found.filled, found.remaining = OrderStatus.CLOSED, filled, entry.quantity
        old_client_id = entry.client_order_id
        sent_ids = []
        real_execute = stack.execution.execute_market_order

        async def execute(side, pair, quantity, price, client_order_id):
            sent_ids.append(client_order_id)
            return await real_execute(side, pair, quantity, price, client_order_id)

        stack.execution.execute_market_order = execute

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        [new_client_id] = sent_ids
        assert new_client_id != old_client_id
        assert stack.balances.crypto_balance == pytest.approx(entry.quantity * 0.999)
        assert len(stack.journal) == 0

    async def test_an_order_found_in_an_unknown_status_without_a_fill_is_neither_booked_nor_sent_again(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        entry = await self._lose_the_answer(stack)
        found = stack.execution.orders[stack.execution.client_ids[entry.client_order_id]]
        found.status, found.filled, found.remaining = OrderStatus.UNKNOWN, 0.0, entry.quantity
        market_calls = stack.execution.calls.count("execute_market_order")

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is False

        assert stack.execution.calls.count("execute_market_order") == market_calls
        assert stack.balances.crypto_balance == 0.0
        [kept] = stack.journal.entries()
        assert kept.client_order_id == entry.client_order_id
        assert kept.sent

    async def test_a_rounding_refusal_is_dust_and_marks_the_purchase_done(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())

        def refuse(pair, amount):
            raise OrderRejectedError("amount below the market precision")

        stack.execution.round_amount = refuse

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert self._market_orders(stack) == []
        assert len(stack.journal) == 0
        assert NotificationType.ORDER_FAILED in _sent(stack)

    async def test_a_remainder_the_exchange_cannot_round_ends_the_purchase(self):
        stack = build_stack(crypto=0.0, store=RecordingStore())
        quantity = self._quantity(stack)
        stack.execution.partial_market_fill = 0.5
        real_round = stack.execution.round_amount
        rounds = []

        def round_amount(pair, amount):
            rounds.append(amount)
            if len(rounds) > 1:
                raise OrderRejectedError("amount below the market precision")
            return real_round(pair, amount)

        stack.execution.round_amount = round_amount

        assert await stack.manager.perform_initial_purchase(ARMING_PRICE) is True

        assert len(self._market_orders(stack)) == 1
        assert stack.balances.crypto_balance == pytest.approx(quantity * 0.5 * 0.999)
        assert len(stack.journal) == 0
        assert stack.manager.arming.initial_purchase_done is True


class FakeClock:
    """Stands in for `OrderManager._clock` and `_sleep`: sleeping only moves the time forward."""

    def __init__(self, now: float = 1000.0):
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _liquidating_stack(crypto: float = 2.0, fiat: float = 0.0, store=None):
    stack = build_stack(fiat=fiat, crypto=crypto, store=store)
    clock = FakeClock()
    stack.manager._clock = clock
    stack.manager._sleep = clock.sleep
    return stack, clock


def _liquidation_alerts(stack) -> list[str]:
    return [
        call.kwargs["error_details"]
        for call in stack.notifications.async_send_notification.await_args_list
        if call.args
        and call.args[0] is NotificationType.ERROR_OCCURRED
        and "iquidation" in call.kwargs.get("error_details", "")
    ]


def _market_sells(stack) -> list[float]:
    return [
        order.amount
        for order in stack.execution.orders.values()
        if order.order_type is OrderType.MARKET and order.side is OrderSide.SELL
    ]


def _stop_loss_report(stack) -> str:
    [details] = [
        call.kwargs["order_details"]
        for call in stack.notifications.async_send_notification.await_args_list
        if call.args and call.args[0] is NotificationType.STOP_LOSS_TRIGGERED
    ]
    return details


class TestStopLossReport:
    async def test_a_full_sale_reports_the_position_liquidated(self):
        stack, _ = _liquidating_stack(crypto=2.0)

        await stack.manager.execute_take_profit_or_stop_loss_order(ARMING_PRICE, stop_loss_order=True)

        assert "position liquidated" in _stop_loss_report(stack)

    async def test_a_liquidation_stopped_on_dust_reports_what_is_left(self):
        stack, _ = _liquidating_stack(crypto=0.001)
        stack.manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=1000.0))

        await stack.manager.execute_take_profit_or_stop_loss_order(ARMING_PRICE, stop_loss_order=True)

        report = _stop_loss_report(stack)
        assert "position liquidated" not in report
        assert "liquidation stopped with 0.001 BTC left" in report

    async def test_an_exchange_holding_less_than_the_books_is_alerted_and_reported(self):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.manager.exchange_balance = AsyncMock(return_value=0.0)
        stack.execution.market_order_failures = [OrderRejectedError("insufficient balance")]

        await stack.manager.execute_take_profit_or_stop_loss_order(ARMING_PRICE, stop_loss_order=True)

        assert any("the books still show 2.0 BTC" in alert for alert in _liquidation_alerts(stack))
        assert "liquidation stopped with 2.0 BTC left" in _stop_loss_report(stack)


class TestLiquidation:
    async def test_a_liquidation_sells_the_free_crypto_and_books_it(self):
        stack, clock = _liquidating_stack(crypto=2.0)

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert stack.balances.crypto_balance == 0.0
        assert stack.balances.balance == pytest.approx(2.0 * ARMING_PRICE * (1 - 0.001))
        assert stack.manager._liquidated
        assert len(stack.journal) == 0
        assert clock.sleeps == []

    async def test_closing_without_liquidation_cancels_the_grid_and_keeps_the_crypto(self):
        stack, _ = _liquidating_stack(crypto=20.0, fiat=2700.0)
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        crypto_total = stack.balances.get_adjusted_crypto_balance()

        await stack.manager.close_positions(liquidate=False)

        assert stack.execution.open_orders() == []
        assert _market_sells(stack) == []
        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_total)
        assert reserved_matches_open_orders(stack)

    async def test_a_second_close_after_a_liquidation_does_nothing(self):
        stack, _ = _liquidating_stack(crypto=1.0)
        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)
        calls = list(stack.execution.calls)

        await stack.manager.close_positions(liquidate=False)
        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert stack.execution.calls == calls

    async def test_a_failed_cancellation_still_liquidates_and_alerts(self):
        stack, _ = _liquidating_stack(crypto=20.0, fiat=2700.0)
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        # The exchange ended this buy behind the bot's back, so its cancellation fails.
        stack.execution.end(stack.execution.order_at(100.0, OrderSide.BUY).identifier, OrderStatus.CANCELED)

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) != []
        alerts = [
            call.kwargs["error_details"]
            for call in stack.notifications.async_send_notification.await_args_list
            if call.args and call.args[0] is NotificationType.ERROR_OCCURRED
        ]
        assert "Shutdown: some open grid orders could not be cancelled on the exchange." in alerts

    async def test_a_close_never_deadlocks_on_the_cancellation_lock(self):
        stack, _ = _liquidating_stack(crypto=20.0, fiat=2700.0)
        await stack.manager.initialize_grid_orders(ARMING_PRICE)

        await asyncio.wait_for(stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE), timeout=2)

        assert stack.manager._liquidated

    async def test_nothing_to_sell_sends_no_market_order(self):
        stack, _ = _liquidating_stack(crypto=0.0)

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == []
        assert _liquidation_alerts(stack) == []
        assert stack.manager._liquidated

    async def test_a_position_under_the_exchange_minimum_stops_with_one_alert(self):
        stack, _ = _liquidating_stack(crypto=0.001)
        stack.manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=1000.0))

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)
        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == []
        alerts = _liquidation_alerts(stack)
        assert len(alerts) == 1
        assert "Liquidation stopped: 0.001 BTC left, under the exchange minimum" in alerts[0]
        assert stack.manager._liquidated

    async def test_a_remainder_the_exchange_cannot_round_stops_like_dust(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.partial_market_fill = 0.5
        real_round = stack.execution.round_amount
        sold = []

        def round_amount(pair, amount):
            if sold:
                raise OrderRejectedError("amount below the market precision")
            return real_round(pair, amount)

        async def execute(*args, **kwargs):
            order = await real_execute(*args, **kwargs)
            sold.append(order.filled)
            return order

        real_execute = stack.execution.execute_market_order
        stack.execution.round_amount = round_amount
        stack.execution.execute_market_order = execute

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert stack.balances.crypto_balance == pytest.approx(1.0)
        assert clock.sleeps == []
        assert len(stack.journal) == 0
        alerts = _liquidation_alerts(stack)
        assert len(alerts) == 1
        assert "Liquidation stopped: 1.0 BTC left, which the exchange cannot round" in alerts[0]
        assert stack.manager._liquidated

    async def test_transient_failures_are_retried_with_a_growing_delay_until_the_sale(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 5

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert clock.sleeps == [1, 2, 4, 8, 16]
        assert _market_sells(stack) == [2.0]
        assert stack.balances.crypto_balance == 0.0
        alerts = _liquidation_alerts(stack)
        assert len(alerts) == 1
        assert "retrying until it succeeds" in alerts[0]

    async def test_the_delay_stops_growing_at_30_s_and_the_alert_repeats_every_10_minutes(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 25

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert clock.sleeps == [1, 2, 4, 8, 16] + [30] * 20
        alerts = _liquidation_alerts(stack)
        assert len(alerts) == 2
        assert "still failing after 25 attempts" in alerts[1]
        assert stack.manager._liquidated

    async def test_a_refusal_rereads_the_exchange_balance_and_sells_what_it_holds(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.manager.exchange_balance = AsyncMock(return_value=1.5)
        stack.execution.market_order_failures = [OrderRejectedError("insufficient balance")]

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        stack.manager.exchange_balance.assert_awaited_once()
        assert _market_sells(stack) == [1.5]
        assert clock.sleeps == [1]
        assert stack.balances.crypto_balance == pytest.approx(0.5)
        assert stack.manager._liquidated

    @pytest.mark.parametrize("read_error", [TypeError("must be real number, not NoneType"), DataFetchError("down")])
    async def test_a_failing_balance_read_after_a_refusal_keeps_the_liquidation_going(self, read_error):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.manager.exchange_balance = AsyncMock(side_effect=read_error)
        stack.execution.market_order_failures = [OrderRejectedError("insufficient balance")]

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert clock.sleeps == [1]
        assert stack.manager._liquidated

    async def test_an_unexpected_error_of_an_attempt_is_a_failed_attempt_retried_with_an_alert(self, caplog):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [RuntimeError("boom"), RuntimeError("boom")]

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert clock.sleeps == [1, 2]
        assert stack.manager._liquidated
        assert len(_liquidation_alerts(stack)) == 1
        assert any(
            record.levelno == logging.ERROR and record.exc_info and "attempt 1" in record.getMessage()
            for record in caplog.records
        )

    async def test_the_first_attempt_runs_even_when_already_interrupted(self):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.manager.interrupt_liquidation()

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert stack.manager._liquidated

    async def test_an_interruption_ends_the_retries_after_the_first_attempt_with_an_alert(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 3
        stack.manager.interrupt_liquidation()

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert clock.sleeps == []
        assert len(stack.execution.market_order_failures) == 2
        assert stack.balances.crypto_balance == 2.0
        assert not stack.manager._liquidated
        assert "Liquidation interrupted by the shutdown, 2.0 BTC still open." in _liquidation_alerts(stack)[-1]

    async def test_an_interruption_during_a_wait_ends_the_retries(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 3

        async def interrupted_sleep(delay):
            await clock.sleep(delay)
            stack.manager.interrupt_liquidation()

        stack.manager._sleep = interrupted_sleep

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert clock.sleeps == [1]
        assert len(stack.execution.market_order_failures) == 2
        assert not stack.manager._liquidated

    async def test_an_interrupted_liquidation_runs_again_when_asked_again(self):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")]
        stack.manager.interrupt_liquidation()
        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert stack.manager._liquidated

    async def test_a_partial_leg_is_booked_and_the_rest_sold_at_once(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.partial_market_fill = 0.4

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0, pytest.approx(1.2)]
        assert clock.sleeps == []
        assert stack.balances.crypto_balance == pytest.approx(0.0, abs=1e-8)
        assert stack.manager._liquidated

    async def test_a_lost_answer_is_found_by_its_client_id_before_selling_again(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        execute = stack.execution.execute_market_order
        lost = []

        async def answer_lost_once(*args, **kwargs):
            order = await execute(*args, **kwargs)
            if not lost:
                lost.append(order.identifier)
                raise OrderOutcomeUnknownError("the answer never came")
            return order

        stack.execution.execute_market_order = answer_lost_once

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert stack.balances.crypto_balance == 0.0
        assert clock.sleeps == [1]
        assert len(stack.journal) == 0
        assert stack.manager._liquidated

    async def test_a_lost_order_found_still_open_is_looked_up_again_not_sold_again(self):
        stack, clock = _liquidating_stack(crypto=2.0)
        execute = stack.execution.execute_market_order
        lost = []

        async def answer_lost_once(*args, **kwargs):
            order = await execute(*args, **kwargs)
            if not lost:
                lost.append(order.identifier)
                # The exchange still works the order and has filled nothing yet when the bot looks it up.
                held = stack.execution.orders[order.identifier]
                held.status, held.filled, held.remaining = OrderStatus.OPEN, 0.0, held.amount
                raise OrderOutcomeUnknownError("the answer never came")
            return order

        async def find_order(client_order_id, pair):
            found = await real_find(client_order_id, pair)
            held = stack.execution.orders[lost[0]]
            held.status, held.filled, held.remaining = OrderStatus.CLOSED, held.amount, 0.0
            return found

        real_find = stack.execution.find_order
        stack.execution.execute_market_order = answer_lost_once
        stack.execution.find_order = find_order

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == [2.0]
        assert stack.balances.crypto_balance == 0.0
        assert clock.sleeps == [1, 2]
        assert len(stack.journal) == 0
        assert stack.manager._liquidated

    @pytest.mark.parametrize("filled", [0.0, None])
    async def test_a_lost_order_found_closed_without_a_fill_is_sold_again_under_a_new_client_id(self, filled):
        stack, _ = _liquidating_stack(crypto=2.0)
        execute = stack.execution.execute_market_order
        sent_ids = []

        async def answer_lost_once(side, pair, quantity, price, client_order_id):
            sent_ids.append(client_order_id)
            order = await execute(side, pair, quantity, price, client_order_id)
            if len(sent_ids) == 1:
                held = stack.execution.orders[order.identifier]
                held.status, held.filled, held.remaining = OrderStatus.CLOSED, filled, held.amount
                raise OrderOutcomeUnknownError("the answer never came")
            return order

        stack.execution.execute_market_order = answer_lost_once

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        first, second = sent_ids
        assert second != first
        assert stack.balances.crypto_balance == 0.0
        assert len(stack.journal) == 0
        assert stack.manager._liquidated

    async def test_a_close_drops_an_unsent_paired_entry_and_releases_its_funds(self):
        stack, _ = _liquidating_stack(crypto=20.0, fiat=2700.0)
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        stack.execution.fail_next_limit_order = OrderNotPlacedError("exchange unavailable")
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))
        assert len(stack.journal) == 1

        await stack.manager.close_positions(liquidate=False)

        assert len(stack.journal) == 0
        assert stack.balances.reserved_fiat == 0.0
        assert stack.balances.reserved_crypto == 0.0
        assert reserved_matches_open_orders(stack)

    async def test_a_sent_entry_is_resolved_before_the_sale_so_no_order_stays_open(self):
        stack, _ = _liquidating_stack(crypto=20.0, fiat=2700.0)
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        stack.execution.lose_next_limit_response = OrderOutcomeUnknownError("the answer never came")
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        await stack.manager.apply_order_update(stack.execution.fill(buy.identifier))
        assert [entry.sent for entry in stack.journal.entries()] == [True]

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert stack.execution.open_orders() == []
        assert len(stack.journal) == 0
        assert stack.balances.get_adjusted_crypto_balance() == pytest.approx(0.0, abs=1e-8)

    async def test_each_attempt_saves_the_entry_as_sent_before_the_market_order_and_saves_under_the_lock(self):
        stack, _ = _liquidating_stack(crypto=2.0, store=RecordingStore())
        written = []
        locks = []
        save_pending = stack.store.save_pending
        save = stack.store.save

        async def recording_save_pending(entry):
            written.append((entry.purpose, entry.sent, "execute_market_order" in stack.execution.calls))
            return await save_pending(entry)

        async def recording_save(arming):
            locks.append(stack.manager._lock.locked())
            await save(arming)

        stack.store.save_pending = recording_save_pending
        stack.store.save = recording_save

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert written == [(PlacementPurpose.LIQUIDATION, True, False)]
        assert locks == [True, True, True]

    async def test_failed_attempts_and_an_interruption_are_logged(self, caplog):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 3
        stack.manager.interrupt_liquidation()

        with caplog.at_level(logging.WARNING):
            await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(message.startswith("Liquidation attempt 1 failed") for message in warnings)
        assert any(message.startswith("Liquidation interrupted by the shutdown") for message in errors)

    async def test_the_backoff_is_read_from_the_class_at_call_time(self, monkeypatch):
        monkeypatch.setattr(OrderManager, "LIQUIDATION_BACKOFF", (0.2,))
        stack, clock = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 3

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert clock.sleeps == [0.2, 0.2, 0.2]
        assert stack.manager._liquidated

    async def test_an_entry_that_cannot_be_saved_is_not_sent(self):
        store = RecordingStore()
        store.fail_pending = True
        stack, _ = _liquidating_stack(crypto=2.0, store=store)
        stack.manager.interrupt_liquidation()

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == []
        assert "execute_market_order" not in stack.execution.calls
        assert not stack.manager._liquidated

    @pytest.mark.parametrize(
        "error", [OrderNotPlacedError("exchange unavailable"), OrderRejectedError("insufficient balance")]
    )
    async def test_the_state_saved_after_a_failed_attempt_still_holds_the_liquidation_entry(self, error):
        stack, _ = _liquidating_stack(crypto=2.0, store=RecordingStore())
        stack.store.observe = lambda: [(entry.purpose, entry.sent) for entry in stack.journal.entries()]
        stack.execution.market_order_failures = [error]
        stack.manager.interrupt_liquidation()

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert stack.store.observed[-1] == [(PlacementPurpose.LIQUIDATION, False)]
        assert not stack.manager._liquidated

    async def test_a_leg_the_exchange_never_received_is_sent_again_under_the_same_client_id(self):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderNotPlacedError("exchange unavailable")] * 2
        sent_ids = []
        real_execute = stack.execution.execute_market_order

        async def execute(side, pair, quantity, price, client_order_id):
            sent_ids.append(client_order_id)
            return await real_execute(side, pair, quantity, price, client_order_id)

        stack.execution.execute_market_order = execute

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert len(sent_ids) == 3
        assert len(set(sent_ids)) == 1
        assert _market_sells(stack) == [2.0]
        assert len(stack.journal) == 0

    async def test_a_leg_that_ended_without_a_fill_is_sent_again_under_a_new_client_id(self):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.execution.partial_market_fill = 0.0
        sent_ids = []
        real_execute = stack.execution.execute_market_order

        async def execute(side, pair, quantity, price, client_order_id):
            sent_ids.append(client_order_id)
            return await real_execute(side, pair, quantity, price, client_order_id)

        stack.execution.execute_market_order = execute

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        first, second = sent_ids
        assert second != first
        assert stack.balances.crypto_balance == 0.0
        assert len(stack.journal) == 0

    async def test_a_refused_leg_is_kept_and_sent_again_for_what_the_exchange_holds(self):
        stack, _ = _liquidating_stack(crypto=2.0, store=RecordingStore())
        stack.manager.exchange_balance = AsyncMock(return_value=1.5)
        stack.execution.market_order_failures = [OrderRejectedError("insufficient balance")]
        sent = []
        real_execute = stack.execution.execute_market_order

        async def execute(side, pair, quantity, price, client_order_id):
            sent.append((quantity, [(e.client_order_id, e.quantity) for e in stack.journal.entries()]))
            return await real_execute(side, pair, quantity, price, client_order_id)

        stack.execution.execute_market_order = execute

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        [(first_quantity, _), (second_quantity, [(_, entry_quantity)])] = sent
        assert (first_quantity, second_quantity, entry_quantity) == (2.0, 1.5, 1.5)
        assert [entry.purpose for entry in stack.store.pending] == [PlacementPurpose.LIQUIDATION] * 2
        assert len(stack.journal) == 0

    @pytest.mark.parametrize("crypto", [0.001, 0.0])
    async def test_a_kept_entry_is_removed_when_the_liquidation_ends_without_a_sale(self, crypto):
        stack, _ = _liquidating_stack(crypto=2.0)
        stack.execution.market_order_failures = [OrderRejectedError("insufficient balance")]
        stack.manager.exchange_balance = AsyncMock(return_value=crypto)
        stack.manager.set_market_constraints(MarketConstraints(min_amount=None, min_cost=1.0))

        await stack.manager.close_positions(liquidate=True, current_price=ARMING_PRICE)

        assert _market_sells(stack) == []
        assert len(stack.journal) == 0
        assert stack.manager._liquidated


async def _ended_while_down(stack, price, side):
    """Settles the order at `price` the way recovery does for an order the exchange cancelled while the bot was down."""
    exchange_view = stack.execution.end(stack.execution.order_at(price, side).identifier, OrderStatus.CANCELED)
    local = stack.book.get_order(exchange_view.identifier)
    grid_level = stack.book.get_grid_level_for_order(local)
    local.apply_update(exchange_view)
    stack.book.remove_open_order(local)
    await stack.balances.record_execution(local, 0.0)
    stack.grid.rollback_order_placement(grid_level, side, False)
    return local, grid_level


class TestReplacementsForRecoveredEnds:
    async def test_each_order_ended_while_down_is_replaced_on_its_level(self):
        stack = build_stack()
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        ended = await _ended_while_down(stack, 90.0, OrderSide.BUY)

        await stack.manager.place_replacements_for_recovered_ends([ended])

        replacement = stack.execution.order_at(90.0, OrderSide.BUY)
        assert replacement.identifier != ended[0].identifier
        assert replacement.amount == pytest.approx(ended[0].remaining)
        assert stack.book.get_order(replacement.identifier).is_open()
        assert reserved_matches_open_orders(stack)

    async def test_a_refused_replacement_alerts_and_leaves_the_level_free(self):
        stack = build_stack()
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        ended = await _ended_while_down(stack, 90.0, OrderSide.BUY)
        stack.balances._balance = Decimal("0")

        await stack.manager.place_replacements_for_recovered_ends([ended])

        assert not stack.execution.open_orders(OrderSide.BUY) or all(
            order.price != 90.0 for order in stack.execution.open_orders(OrderSide.BUY)
        )
        assert len(stack.journal) == 0
        assert stack.grid.grid_levels[90.0].state == stack.grid.grid_levels[90.0].home_state
        failures = [
            call
            for call in stack.notifications.async_send_notification.await_args_list
            if call.args and call.args[0] is NotificationType.ORDER_FAILED
        ]
        assert len(failures) == 1

    async def test_no_replacement_once_the_positions_are_closed(self):
        stack = build_stack()
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        ended = await _ended_while_down(stack, 90.0, OrderSide.BUY)
        stack.manager._positions_closed = True

        await stack.manager.place_replacements_for_recovered_ends([ended])

        assert all(order.price != 90.0 for order in stack.execution.open_orders(OrderSide.BUY))
