import logging
import re

import pytest
from tests.fakes.recording_store import RecordingStore
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.bot_management.notification.notification_content import NotificationType
from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling import order_manager as order_manager_module
from grid_trading_bot.core.order_handling.exceptions import OrderExecutionFailedError
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
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
