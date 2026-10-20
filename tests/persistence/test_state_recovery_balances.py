from dataclasses import replace
from decimal import Decimal
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.order_handling.placement_journal import (
    PendingPlacement,
    PlacementPurpose,
    new_client_order_id,
)
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.persistence.serializers import pending_placement_to_dict
from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_recovery_service import RecoveryOutcome, StateRecoveryService
from grid_trading_bot.core.services.exceptions import DataFetchError, OrderNotPlacedError, OrderRejectedError

FEE = 0.001


@pytest.fixture
def repository():
    repository = SQLiteStateRepository(db_path=":memory:")
    repository.initialize()
    yield repository
    repository.close()


def _store(repository, stack) -> StatePersistenceService:
    return StatePersistenceService(
        repository,
        EventBus(),
        stack.notifications,
        stack.book,
        stack.grid,
        stack.balances,
        stack.config,
        "BTC/USDT",
        "simple_grid",
        journal=stack.journal,
    )


async def _armed_and_saved(repository, trading_mode="live", initial_purchase_done=True, grid_orders_initialized=True):
    stack = build_stack(trading_mode=trading_mode)
    await stack.manager.initialize_grid_orders(ARMING_PRICE)
    await _store(repository, stack).save(ArmingFlags(initial_purchase_done, grid_orders_initialized))
    return stack


def _exchange_balances(before, executions):
    """The free balances an exchange reports once `executions` ((side, quantity, price)) filled while the bot was
    down: the funds of the orders still open there are locked, so not free."""
    fiat = before.balances.get_adjusted_fiat_balance()
    crypto = before.balances.get_adjusted_crypto_balance()
    for side, quantity, price in executions:
        if side == OrderSide.BUY:
            fiat -= quantity * price
            crypto += quantity * (1 - FEE)
        else:
            fiat += quantity * price * (1 - FEE)
            crypto -= quantity
    open_orders = before.execution.open_orders()
    locked_fiat = sum(o.price * o.remaining for o in open_orders if o.side == OrderSide.BUY)
    locked_crypto = sum(o.remaining for o in open_orders if o.side == OrderSide.SELL)
    return {"free": {"USDT": fiat - locked_fiat, "BTC": crypto - locked_crypto}}, fiat, crypto


async def _restarted(repository, before, exchange_balances, trading_mode="live"):
    after = build_stack(fiat=0.0, crypto=0.0, trading_mode=trading_mode)
    after.execution = before.execution
    after.manager.order_execution_strategy = before.execution
    exchange_service = Mock()
    exchange_service.get_balance = AsyncMock(return_value=exchange_balances)
    exchange_service.fetch_open_orders = AsyncMock(return_value=[])
    recovery = StateRecoveryService(
        repository,
        after.config,
        after.grid,
        after.book,
        after.balances,
        exchange_service,
        before.execution,
        after.notifications,
        "BTC/USDT",
        journal=after.journal,
    )
    result = await recovery.attempt_recovery()
    return after, result, exchange_service


CONFIG_HASH = "3f2a9c1d"


def _limit_entry(
    level_price=110.0,
    quantity=0.5,
    side=OrderSide.SELL,
    source_level_price=100.0,
    sent=True,
    purpose=PlacementPurpose.PAIRED,
    source_order_id=None,
):
    return PendingPlacement(
        client_order_id=new_client_order_id(CONFIG_HASH),
        order_type=OrderType.LIMIT,
        side=side,
        quantity=quantity,
        price=level_price,
        purpose=purpose,
        level_price=level_price,
        source_level_price=source_level_price,
        source_order_id=source_order_id,
        sent=sent,
        created_at=1,
    )


def _market_entry(purpose, side, quantity):
    return PendingPlacement(
        client_order_id=new_client_order_id(CONFIG_HASH),
        order_type=OrderType.MARKET,
        side=side,
        quantity=quantity,
        price=ARMING_PRICE,
        purpose=purpose,
        sent=True,
        created_at=1,
    )


def _saved_entry(repository, entry):
    """Writes `entry` the way the placement helper does just before sending it, after the last full save."""
    repository.save_pending(pending_placement_to_dict(entry))
    return entry


async def _landed(before, entry):
    """The order `entry` placed on the exchange before the bot stopped."""
    return await before.execution.execute_limit_order(
        entry.side, "BTC/USDT", entry.quantity, entry.price, client_order_id=entry.client_order_id
    )


class TestLiveRecoveryBalances:
    async def test_a_partial_fill_seen_at_restart_is_booked_once(self, repository):
        before = await _armed_and_saved(repository)
        buy = before.execution.order_at(100.0, OrderSide.BUY)
        before.execution.fill(buy.identifier, buy.amount * 0.4)
        balances, fiat, crypto = _exchange_balances(before, [(OrderSide.BUY, buy.amount * 0.4, 100.0)])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert after.book.get_order(buy.identifier).filled == pytest.approx(buy.amount * 0.4)
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat)
        assert reserved_matches_open_orders(after)

        await after.manager.apply_order_update(before.execution.fill(buy.identifier))

        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto + buy.amount * 0.6 * (1 - FEE))

    @pytest.mark.parametrize("fee", [{"cost": 0.1, "currency": "USDT"}, {"cost": 0.0005, "currency": "BNB"}])
    async def test_a_final_fee_in_another_currency_than_the_partial_booked_at_restart_keeps_the_model(
        self, repository, caplog, fee
    ):
        caplog.set_level(logging.INFO)
        before = await _armed_and_saved(repository)
        fees_before = before.balances.total_fees
        buy = before.execution.order_at(100.0, OrderSide.BUY)
        before.execution.fill(buy.identifier, buy.amount * 0.4)
        balances, _, crypto = _exchange_balances(before, [(OrderSide.BUY, buy.amount * 0.4, 100.0)])
        after, _, _ = await _restarted(repository, before, balances)
        view = before.execution.fill(buy.identifier)
        view.fee = fee

        await after.manager.apply_order_update(view)

        credited = after.balances.get_adjusted_crypto_balance() - (crypto - buy.amount * 0.4 * (1 - FEE))
        assert credited == pytest.approx(buy.amount * (1 - FEE))
        assert after.balances.total_fees == pytest.approx(fees_before + buy.amount * 100.0 * FEE)
        local = after.book.get_order(buy.identifier)
        assert buy.amount - after.balances.fee_calculator.order_base_fee(local) == pytest.approx(credited)
        assert after.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(credited)
        reported = f"{fee['cost']} {fee['currency']}"
        assert [r.levelname for r in caplog.records if reported in r.getMessage()] == ["INFO"]

    async def test_a_fill_while_down_reaches_the_fees_and_the_cost_basis_once(self, repository):
        before = await _armed_and_saved(repository)
        fees_before = before.balances.total_fees
        buy = before.execution.order_at(100.0, OrderSide.BUY)
        before.execution.fill(buy.identifier)
        balances, _, crypto = _exchange_balances(before, [(OrderSide.BUY, buy.amount, 100.0)])

        after, result, _ = await _restarted(repository, before, balances)

        assert [order.identifier for order, _ in result.orders_filled_while_down] == [buy.identifier]
        assert after.balances.total_fees == pytest.approx(fees_before + buy.amount * 100.0 * FEE)
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert after.grid.grid_levels[100.0].state == GridCycleState.READY_TO_BUY

    async def test_an_order_expired_while_down_frees_its_level(self, repository):
        before = await _armed_and_saved(repository)
        buy = before.execution.order_at(90.0, OrderSide.BUY)
        before.execution.end(buy.identifier, OrderStatus.EXPIRED)
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert not after.book.get_order(buy.identifier).is_open()
        assert after.grid.grid_levels[90.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(after)
        assert [(order.identifier, level.price) for order, level in result.ended_while_down] == [(buy.identifier, 90.0)]

    async def test_an_order_the_exchange_no_longer_knows_frees_its_level(self, repository):
        before = await _armed_and_saved(repository)
        sell = before.execution.order_at(120.0, OrderSide.SELL)
        del before.execution.orders[sell.identifier]
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.ghost_orders_found == 1
        assert after.grid.grid_levels[120.0].state == GridCycleState.READY_TO_SELL
        assert reserved_matches_open_orders(after)


class TestPaperRecoveryBalances:
    async def test_paper_balances_resume_from_the_database_and_book_the_fills_once(self, repository):
        before = await _armed_and_saved(repository, trading_mode="paper_trading")
        fiat_total = before.balances.get_adjusted_fiat_balance()
        crypto_total = before.balances.get_adjusted_crypto_balance()
        filled = before.execution.order_at(100.0, OrderSide.BUY)
        partial = before.execution.order_at(90.0, OrderSide.BUY)
        cancelled = before.execution.order_at(80.0, OrderSide.BUY)
        before.execution.fill(filled.identifier)
        before.execution.fill(partial.identifier, partial.amount * 0.4)
        before.execution.end(cancelled.identifier, OrderStatus.CANCELED)

        after, result, exchange_service = await _restarted(repository, before, {"free": {}}, "paper_trading")

        assert result.balance_source == "db"
        exchange_service.get_balance.assert_not_awaited()
        bought = (filled.amount + partial.amount * 0.4) * (1 - FEE)
        cost = filled.amount * 100.0 + partial.amount * 0.4 * 90.0
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_total + bought)
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_total - cost)
        assert reserved_matches_open_orders(after)

        await after.manager.apply_order_update(before.execution.fill(partial.identifier))

        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(
            crypto_total + (filled.amount + partial.amount) * (1 - FEE)
        )


class TestJournalRecovery:
    async def test_a_sent_entry_found_open_is_adopted_on_its_level(self, repository):
        before = await _armed_and_saved(repository)
        entry = _saved_entry(repository, _limit_entry())
        placed = await _landed(before, entry)
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        adopted = after.book.get_order(placed.identifier)
        assert adopted is not None
        assert adopted.is_open()
        assert after.book.get_grid_level_for_order(adopted) is after.grid.grid_levels[110.0]
        assert after.grid.grid_levels[110.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert after.grid.grid_levels[100.0].paired_sell_level is after.grid.grid_levels[110.0]
        assert len(after.journal) == 0
        assert reserved_matches_open_orders(after)
        assert not result.liquidation_in_progress

    async def test_a_sent_entry_found_filled_is_booked_once_and_paired_after_the_restart(self, repository):
        before = await _armed_and_saved(repository)
        entry = _saved_entry(repository, _limit_entry())
        placed = await _landed(before, entry)
        before.execution.fill(placed.identifier)
        balances, fiat, crypto = _exchange_balances(before, [(OrderSide.SELL, entry.quantity, entry.price)])

        after, result, _ = await _restarted(repository, before, balances)

        filled = after.book.get_order(placed.identifier)
        assert [order for order, _ in result.orders_filled_while_down] == [filled]
        assert not filled.is_open()
        assert after.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat)
        assert len(after.journal) == 0

    async def test_a_sent_entry_the_exchange_never_received_is_sent_again_with_its_id(self, repository):
        before = await _armed_and_saved(repository)
        entry = _saved_entry(repository, _limit_entry())
        balances, _, crypto = _exchange_balances(before, [])

        after, _, _ = await _restarted(repository, before, balances)

        restored = after.journal.get(entry.client_order_id)
        assert restored is not None
        assert not restored.sent
        assert after.grid.grid_levels[110.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert reserved_matches_open_orders(after)

        await after.manager.retry_pending_placements()

        placed_id = before.execution.client_ids[entry.client_order_id]
        assert after.book.get_order(placed_id).is_open()
        assert len(after.journal) == 0
        assert reserved_matches_open_orders(after)

    async def test_a_sent_entry_found_cancelled_is_left_for_a_replacement(self, repository):
        before = await _armed_and_saved(repository)
        entry = _saved_entry(repository, _limit_entry())
        placed = await _landed(before, entry)
        before.execution.end(placed.identifier, OrderStatus.CANCELED)
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert [(order.identifier, level.price) for order, level in result.ended_while_down] == [
            (placed.identifier, 110.0)
        ]
        assert after.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert len(after.journal) == 0
        assert reserved_matches_open_orders(after)

    async def test_a_sent_entry_found_rejected_is_reported_and_not_replaced(self, repository):
        before = await _armed_and_saved(repository)
        entry = _saved_entry(repository, _limit_entry())
        placed = await _landed(before, entry)
        before.execution.end(placed.identifier, OrderStatus.REJECTED)
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.ended_while_down == []
        assert any(placed.identifier in line for line in result.not_replaced)
        assert after.grid.grid_levels[110.0].state == GridCycleState.READY_TO_SELL
        assert len(after.journal) == 0

    async def test_an_initial_purchase_found_filled_is_booked_and_marked_done(self, repository):
        before = await _armed_and_saved(repository, initial_purchase_done=False, grid_orders_initialized=False)
        entry = _saved_entry(repository, _market_entry(PlacementPurpose.INITIAL_PURCHASE, OrderSide.BUY, 1.0))
        bought = await before.execution.execute_market_order(
            OrderSide.BUY, "BTC/USDT", 1.0, ARMING_PRICE, client_order_id=entry.client_order_id
        )
        fees_before = before.balances.total_fees
        balances, _, crypto = _exchange_balances(before, [(OrderSide.BUY, 1.0, ARMING_PRICE)])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.initial_purchase_done
        assert after.book.get_order(bought.identifier) is not None
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert after.balances.total_fees > fees_before
        assert len(after.journal) == 0

    async def test_a_market_entry_the_exchange_never_received_is_dropped(self, repository):
        before = await _armed_and_saved(repository, initial_purchase_done=False, grid_orders_initialized=False)
        _saved_entry(repository, _market_entry(PlacementPurpose.INITIAL_PURCHASE, OrderSide.BUY, 1.0))
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert not result.initial_purchase_done
        assert len(after.journal) == 0

    async def test_an_unreadable_entry_defers_the_recovery(self, repository):
        before = await _armed_and_saved(repository)
        _saved_entry(repository, _limit_entry())
        before.execution.find_order = AsyncMock(side_effect=DataFetchError("exchange down"))
        balances, _, _ = _exchange_balances(before, [])

        _, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.DEFERRED

    async def test_an_entry_for_a_level_the_grid_no_longer_has_is_dropped(self, repository, caplog):
        before = await _armed_and_saved(repository)
        _saved_entry(repository, _limit_entry(level_price=105.0, source_level_price=None, sent=False))
        balances, _, crypto = _exchange_balances(before, [])

        with caplog.at_level(logging.WARNING):
            after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert len(after.journal) == 0
        assert "no level at 105.0" in caplog.text
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert reserved_matches_open_orders(after)

    async def test_unsent_entries_survive_a_restart_and_are_sent(self, repository):
        before = build_stack()
        await before.manager.initialize_grid_orders(ARMING_PRICE)
        before.execution.fail_next_limit_order = OrderNotPlacedError("exchange unavailable")
        async with before.manager._locked():
            await before.manager._submit_limit(
                OrderSide.SELL,
                before.grid.grid_levels[110.0],
                0.5,
                PlacementPurpose.PAIRED,
                source_level=before.grid.grid_levels[100.0],
            )
        [entry] = before.journal.entries()
        assert not entry.sent
        await _store(repository, before).save(ArmingFlags(initial_purchase_done=True, grid_orders_initialized=True))
        balances, _, _ = _exchange_balances(before, [])

        after, _, _ = await _restarted(repository, before, balances)

        assert after.journal.get(entry.client_order_id) is not None
        assert reserved_matches_open_orders(after)

        await after.manager.retry_pending_placements()

        sell = before.execution.order_at(110.0, OrderSide.SELL)
        assert after.book.get_order(sell.identifier).is_open()
        assert len(after.journal) == 0
        assert reserved_matches_open_orders(after)

    @pytest.mark.parametrize("fate", ["filled", "cancelled"])
    async def test_a_level_a_restored_entry_aims_at_stays_held_when_its_saved_order_settles(self, repository, fate):
        before = await _armed_and_saved(repository)
        buy = before.execution.order_at(90.0, OrderSide.BUY)
        fills = []
        if fate == "filled":
            before.execution.fill(buy.identifier)
            fills = [(OrderSide.BUY, buy.amount, 90.0)]
        else:
            before.execution.end(buy.identifier, OrderStatus.CANCELED)
        _saved_entry(
            repository, _limit_entry(level_price=90.0, side=OrderSide.BUY, source_level_price=None, sent=False)
        )
        balances, _, _ = _exchange_balances(before, fills)

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert after.grid.grid_levels[90.0].state == GridCycleState.WAITING_FOR_BUY_FILL

    async def test_a_liquidation_found_filled_is_booked_once_and_never_sent_again(self, repository):
        before = await _armed_and_saved(repository)
        entry = _saved_entry(repository, _market_entry(PlacementPurpose.LIQUIDATION, OrderSide.SELL, 1.0))
        sold = await before.execution.execute_market_order(
            OrderSide.SELL, "BTC/USDT", 1.0, ARMING_PRICE, client_order_id=entry.client_order_id
        )
        fees_before = before.balances.total_fees
        balances, fiat, crypto = _exchange_balances(before, [(OrderSide.SELL, 1.0, ARMING_PRICE)])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert not after.book.get_order(sold.identifier).is_open()
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat)
        assert after.balances.total_fees == pytest.approx(fees_before + ARMING_PRICE * FEE)
        assert before.execution.calls.count("execute_market_order") == 1
        assert len(after.journal) == 0
        assert result.liquidation_in_progress

    async def test_a_market_entry_still_open_on_the_exchange_defers_the_recovery(self, repository):
        before = await _armed_and_saved(repository, initial_purchase_done=False, grid_orders_initialized=False)
        entry = _saved_entry(repository, _market_entry(PlacementPurpose.INITIAL_PURCHASE, OrderSide.BUY, 1.0))
        before.execution.partial_market_fill = 0.4
        bought = await before.execution.execute_market_order(
            OrderSide.BUY, "BTC/USDT", 1.0, ARMING_PRICE, client_order_id=entry.client_order_id
        )
        before.execution.orders[bought.identifier].status = OrderStatus.OPEN
        balances, _, _ = _exchange_balances(before, [(OrderSide.BUY, 0.4, ARMING_PRICE)])

        _, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.DEFERRED

    @pytest.mark.parametrize("trading_mode", ["live", "paper_trading"])
    async def test_the_first_release_after_a_restart_finds_the_reservation_it_needs(
        self, repository, caplog, trading_mode
    ):
        before = build_stack(fiat=0.0, trading_mode=trading_mode)
        await before.manager.initialize_grid_orders(ARMING_PRICE)
        before.balances._balance = Decimal("10")
        await _store(repository, before).save(ArmingFlags(initial_purchase_done=True, grid_orders_initialized=True))
        # 0.012345678956 * 90 has 11 decimals and rounds up at the tracker's 1e-8 precision.
        entry = _limit_entry(level_price=90.0, quantity=0.012345678956, side=OrderSide.BUY, source_level_price=None)
        _saved_entry(repository, replace(entry, sent=False))
        balances, _, _ = _exchange_balances(before, [])
        after, _, _ = await _restarted(repository, before, balances, trading_mode)
        before.execution.fail_next_limit_order = OrderRejectedError("price filter")

        with caplog.at_level(logging.WARNING):
            await after.manager.retry_pending_placements()

        assert len(after.journal) == 0
        assert "Attempted to release" not in caplog.text
        assert after.balances.reserved_fiat == 0
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(10.0)

    async def test_an_unsent_liquidation_is_reported_and_dropped(self, repository):
        before = await _armed_and_saved(repository)
        entry = _market_entry(PlacementPurpose.LIQUIDATION, OrderSide.SELL, 1.0)
        entry.sent = False
        _saved_entry(repository, entry)
        balances, _, crypto = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert result.liquidation_in_progress
        assert len(after.journal) == 0
        assert "execute_market_order" not in before.execution.calls
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)

    async def test_a_market_entry_closed_without_a_fill_is_dropped(self, repository):
        before = await _armed_and_saved(repository, initial_purchase_done=False, grid_orders_initialized=False)
        entry = _saved_entry(repository, _market_entry(PlacementPurpose.INITIAL_PURCHASE, OrderSide.BUY, 1.0))
        bought = await before.execution.execute_market_order(
            OrderSide.BUY, "BTC/USDT", 1.0, ARMING_PRICE, client_order_id=entry.client_order_id
        )
        closed = before.execution.orders[bought.identifier]
        closed.filled, closed.remaining = 0.0, 1.0
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)

        assert result.outcome is RecoveryOutcome.RESUMED
        assert not result.initial_purchase_done
        assert after.book.get_order(bought.identifier) is None
        assert len(after.journal) == 0


class TestJournalledSuccessors:
    @pytest.mark.parametrize("sent", [True, False])
    async def test_a_fill_whose_pair_is_journalled_is_not_paired_again(self, repository, sent):
        before = await _armed_and_saved(repository)
        buy = before.execution.order_at(100.0, OrderSide.BUY)
        before.execution.fill(buy.identifier)
        # The pair of the 100 buy was written, and maybe sent, then the bot stopped before the full save.
        entry = _saved_entry(
            repository, _limit_entry(quantity=buy.amount * (1 - FEE), sent=sent, source_order_id=buy.identifier)
        )
        if sent:
            await _landed(before, entry)
        balances, _, crypto = _exchange_balances(before, [(OrderSide.BUY, buy.amount, 100.0)])

        after, result, _ = await _restarted(repository, before, balances)
        await after.manager.place_paired_orders_for_recovered_fills(result.orders_filled_while_down)
        await after.manager.retry_pending_placements()

        assert result.orders_filled_while_down == []
        assert [o.price for o in before.execution.open_orders(OrderSide.SELL)].count(110.0) == 1
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto)
        assert len(after.journal) == 0
        assert reserved_matches_open_orders(after)

    @pytest.mark.parametrize("sent", [True, False])
    async def test_an_end_whose_replacement_is_journalled_is_not_replaced_again(self, repository, sent):
        before = await _armed_and_saved(repository)
        sell = before.execution.order_at(120.0, OrderSide.SELL)
        before.execution.end(sell.identifier, OrderStatus.CANCELED)
        entry = _saved_entry(
            repository,
            _limit_entry(
                level_price=120.0,
                quantity=sell.amount,
                source_level_price=None,
                sent=sent,
                purpose=PlacementPurpose.REPLACEMENT,
                source_order_id=sell.identifier,
            ),
        )
        if sent:
            await _landed(before, entry)
        balances, _, _ = _exchange_balances(before, [])

        after, result, _ = await _restarted(repository, before, balances)
        await after.manager.place_replacements_for_recovered_ends(result.ended_while_down)
        await after.manager.retry_pending_placements()

        assert result.ended_while_down == []
        assert [o.price for o in before.execution.open_orders(OrderSide.SELL)].count(120.0) == 1
        assert after.grid.grid_levels[120.0].state == GridCycleState.WAITING_FOR_SELL_FILL
        assert len(after.journal) == 0
        assert reserved_matches_open_orders(after)


class TestPaperJournalRecovery:
    async def test_paper_reservations_cover_an_entry_saved_after_the_last_snapshot(self, repository):
        before = await _armed_and_saved(repository, trading_mode="paper_trading")
        fiat_total = before.balances.get_adjusted_fiat_balance()
        crypto_total = before.balances.get_adjusted_crypto_balance()
        entry = _saved_entry(repository, _limit_entry(sent=False))

        after, _, _ = await _restarted(repository, before, {"free": {}}, "paper_trading")

        assert after.journal.get(entry.client_order_id) is not None
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_total)
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_total)
        assert reserved_matches_open_orders(after)

    async def test_paper_books_an_initial_purchase_found_filled_once(self, repository):
        before = await _armed_and_saved(
            repository, trading_mode="paper_trading", initial_purchase_done=False, grid_orders_initialized=False
        )
        fiat_total = before.balances.get_adjusted_fiat_balance()
        crypto_total = before.balances.get_adjusted_crypto_balance()
        entry = _saved_entry(repository, _market_entry(PlacementPurpose.INITIAL_PURCHASE, OrderSide.BUY, 1.0))
        await before.execution.execute_market_order(
            OrderSide.BUY, "BTC/USDT", 1.0, ARMING_PRICE, client_order_id=entry.client_order_id
        )

        after, result, _ = await _restarted(repository, before, {"free": {}}, "paper_trading")

        assert result.initial_purchase_done
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_total + 1.0 * (1 - FEE))
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_total - ARMING_PRICE)
        assert reserved_matches_open_orders(after)
        assert len(after.journal) == 0

    async def test_paper_books_a_liquidation_found_filled_once(self, repository):
        before = await _armed_and_saved(repository, trading_mode="paper_trading")
        fiat_total = before.balances.get_adjusted_fiat_balance()
        crypto_total = before.balances.get_adjusted_crypto_balance()
        entry = _saved_entry(repository, _market_entry(PlacementPurpose.LIQUIDATION, OrderSide.SELL, 1.0))
        await before.execution.execute_market_order(
            OrderSide.SELL, "BTC/USDT", 1.0, ARMING_PRICE, client_order_id=entry.client_order_id
        )

        after, result, _ = await _restarted(repository, before, {"free": {}}, "paper_trading")

        assert result.outcome is RecoveryOutcome.RESUMED
        assert after.balances.get_adjusted_crypto_balance() == pytest.approx(crypto_total - 1.0)
        assert after.balances.get_adjusted_fiat_balance() == pytest.approx(fiat_total + ARMING_PRICE * (1 - FEE))
        assert reserved_matches_open_orders(after)
        assert len(after.journal) == 0
