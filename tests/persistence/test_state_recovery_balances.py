import logging
from unittest.mock import AsyncMock, Mock

import pytest
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.bot_management.event_bus import EventBus
from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus
from grid_trading_bot.core.order_handling.state_store import ArmingFlags
from grid_trading_bot.core.persistence.sqlite_state_repository import SQLiteStateRepository
from grid_trading_bot.core.persistence.state_persistence_service import StatePersistenceService
from grid_trading_bot.core.persistence.state_recovery_service import RecoveryOutcome, StateRecoveryService

FEE = 0.001


@pytest.fixture
def repository():
    repository = SQLiteStateRepository(db_path=":memory:")
    repository.initialize()
    yield repository
    repository.close()


async def _armed_and_saved(repository, trading_mode="live"):
    stack = build_stack(trading_mode=trading_mode)
    await stack.manager.initialize_grid_orders(ARMING_PRICE)
    store = StatePersistenceService(
        repository,
        EventBus(),
        stack.notifications,
        stack.book,
        stack.grid,
        stack.balances,
        stack.config,
        "BTC/USDT",
        "simple_grid",
    )
    await store.save(ArmingFlags(initial_purchase_done=True, grid_orders_initialized=True))
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
    )
    result = await recovery.attempt_recovery()
    return after, result, exchange_service


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

        after, _, _ = await _restarted(repository, before, balances)

        assert not after.book.get_order(buy.identifier).is_open()
        assert after.grid.grid_levels[90.0].state == GridCycleState.READY_TO_BUY
        assert reserved_matches_open_orders(after)

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
