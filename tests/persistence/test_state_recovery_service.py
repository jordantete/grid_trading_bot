from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from grid_trading_bot.core.grid_management.grid_level import GridCycleState, GridLevel
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.persistence.serializers import compute_config_hash
from grid_trading_bot.core.persistence.state_recovery_service import StateRecoveryService

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_order_dict(
    identifier="order-1",
    status="open",
    side="buy",
    price=100.0,
    amount=1.0,
    remaining=1.0,
    grid_level_price=100.0,
    is_non_grid_order=0,
):
    """Return a minimal order dict as stored by the repository."""
    return {
        "identifier": identifier,
        "status": status,
        "order_type": "limit",
        "side": side,
        "price": price,
        "average": None,
        "amount": amount,
        "filled": 0.0,
        "remaining": remaining,
        "timestamp": 1700000000,
        "datetime_str": None,
        "last_trade_timestamp": None,
        "symbol": "ETH/USDT",
        "time_in_force": "GTC",
        "cost": None,
        "trades_json": None,
        "fee_json": None,
        "info_json": None,
        "grid_level_price": grid_level_price,
        "is_non_grid_order": is_non_grid_order,
    }


def _make_exchange_order(
    identifier="order-1",
    status=OrderStatus.OPEN,
    side=OrderSide.BUY,
    price=100.0,
    amount=1.0,
    filled=0.0,
    remaining=1.0,
    average=None,
    cost=None,
):
    """Return an Order object representing the exchange-side view."""
    return Order(
        identifier=identifier,
        status=status,
        order_type=OrderType.LIMIT,
        side=side,
        price=price,
        average=average,
        amount=amount,
        filled=filled,
        remaining=remaining,
        timestamp=1700000000,
        datetime=None,
        last_trade_timestamp=None,
        symbol="ETH/USDT",
        time_in_force="GTC",
        cost=cost,
    )


def _make_saved_balance(
    fiat="5000",
    crypto="2.5",
    fees="10",
    reserved_fiat="500",
    reserved_crypto="0.5",
):
    return {
        "fiat_balance": fiat,
        "crypto_balance": crypto,
        "total_fees": fees,
        "reserved_fiat": reserved_fiat,
        "reserved_crypto": reserved_crypto,
    }


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_config_manager():
    cm = MagicMock()
    cm.get_grid_settings.return_value = {
        "type": "simple_grid",
        "spacing": "geometric",
        "num_grids": 20,
        "range": {"top": 3100, "bottom": 2850},
    }
    cm.get_pair.return_value = {"base_currency": "ETH", "quote_currency": "USDT"}
    cm.get_base_currency.return_value = "ETH"
    cm.get_quote_currency.return_value = "USDT"
    return cm


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.load_bot_state.return_value = None
    repo.load_grid_levels.return_value = []
    repo.load_all_orders.return_value = []
    repo.load_balance_state.return_value = None
    repo.clear_all.return_value = None
    return repo


@pytest.fixture
def mock_grid_manager():
    gm = MagicMock()
    gm.grid_levels = {}
    return gm


@pytest.fixture
def mock_order_book():
    ob = MagicMock()
    ob.get_open_orders.return_value = []
    ob.add_order.return_value = None
    ob.update_order_status.return_value = None
    ob.remove_open_order.return_value = None
    return ob


@pytest.fixture
def mock_balance_tracker():
    bt = MagicMock()
    bt._balance = Decimal("0")
    bt._crypto_balance = Decimal("0")
    bt._total_fees = Decimal("0")
    bt._reserved_fiat = Decimal("0")
    bt._reserved_crypto = Decimal("0")
    bt.release_reserved_fiat = AsyncMock()
    bt.release_reserved_crypto = AsyncMock()
    return bt


@pytest.fixture
def mock_exchange_service():
    es = MagicMock()
    es.get_balance = AsyncMock(return_value={"free": {"USDT": 5000, "ETH": 2.5}})
    es.fetch_open_orders = AsyncMock(return_value=[])
    return es


@pytest.fixture
def mock_order_execution_strategy():
    oes = MagicMock()
    oes.get_order = AsyncMock(return_value=None)
    return oes


@pytest.fixture
def mock_notification_handler():
    return MagicMock()


@pytest.fixture
def service(
    mock_repository,
    mock_config_manager,
    mock_grid_manager,
    mock_order_book,
    mock_balance_tracker,
    mock_exchange_service,
    mock_order_execution_strategy,
    mock_notification_handler,
):
    return StateRecoveryService(
        repository=mock_repository,
        config_manager=mock_config_manager,
        grid_manager=mock_grid_manager,
        order_book=mock_order_book,
        balance_tracker=mock_balance_tracker,
        exchange_service=mock_exchange_service,
        order_execution_strategy=mock_order_execution_strategy,
        notification_handler=mock_notification_handler,
        trading_pair="ETH/USDT",
    )


def _set_valid_bot_state(mock_repository, mock_config_manager, **overrides):
    """Configure the repository to return a bot_state whose config hash matches the current config."""
    current_hash = compute_config_hash(mock_config_manager)
    bot_state = {
        "config_hash": current_hash,
        "initial_purchase_done": True,
        "grid_orders_initialized": True,
    }
    bot_state.update(overrides)
    mock_repository.load_bot_state.return_value = bot_state


# ── Tests ────────────────────────────────────────────────────────────────


class TestStateRecoveryService:
    # 1. No previous state
    async def test_no_previous_state(self, service, mock_repository):
        """When the repository has no saved bot state, recovery should return immediately
        with recovered=False and leave everything untouched."""
        mock_repository.load_bot_state.return_value = None

        result = await service.attempt_recovery()

        assert result.recovered is False
        assert result.errors == []
        mock_repository.clear_all.assert_not_called()

    # 2. Config hash mismatch
    async def test_config_hash_mismatch(self, service, mock_repository):
        """When the saved config hash does not match the current config, the service
        should clear all persisted state and return recovered=False."""
        mock_repository.load_bot_state.return_value = {
            "config_hash": "stale_hash_that_wont_match",
            "initial_purchase_done": True,
            "grid_orders_initialized": True,
        }

        result = await service.attempt_recovery()

        assert result.recovered is False
        mock_repository.clear_all.assert_called_once()

    # 3. Successful basic recovery
    async def test_successful_recovery_basic(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_grid_manager,
        mock_order_book,
        mock_balance_tracker,
        mock_exchange_service,
    ):
        """A full recovery with matching hash, saved grid levels, saved orders,
        and working exchange balance should result in recovered=True with
        balance_source='exchange'."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        # Set up grid levels (one level at price 3000)
        gl = GridLevel(3000.0, GridCycleState.READY_TO_BUY)
        mock_grid_manager.grid_levels = {3000.0: gl}

        mock_repository.load_grid_levels.return_value = [
            {
                "price": 3000.0,
                "state": "waiting_for_buy_fill",
                "paired_buy_level_price": None,
                "paired_sell_level_price": None,
            },
        ]

        order_dict = _make_order_dict(identifier="ord-1", price=3000.0, grid_level_price=3000.0)
        mock_repository.load_all_orders.return_value = [order_dict]

        mock_repository.load_balance_state.return_value = _make_saved_balance()

        # Exchange: order is still open (get_order returns an OPEN order)
        mock_order_execution_strategy = service.order_execution_strategy
        exchange_order = _make_exchange_order(identifier="ord-1", status=OrderStatus.OPEN)
        mock_order_execution_strategy.get_order = AsyncMock(return_value=exchange_order)

        # The order_book.get_open_orders should reflect the restored order
        restored_order = _make_exchange_order(identifier="ord-1", status=OrderStatus.OPEN)
        mock_order_book.get_open_orders.return_value = [restored_order]

        mock_exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 5000, "ETH": 2.5}})

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.initial_purchase_done is True
        assert result.grid_orders_initialized is True
        assert result.balance_source == "exchange"
        assert result.ghost_orders_found == 0
        assert result.orphan_orders_found == 0
        assert result.errors == []

        # Grid level state should have been restored
        assert gl.state == GridCycleState.WAITING_FOR_BUY_FILL

        # Order should have been added to order_book
        mock_order_book.add_order.assert_called_once()

    # 4. Order filled while bot was down
    async def test_order_filled_while_bot_down(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_order_book,
        mock_exchange_service,
    ):
        """If an order was open in the DB but the exchange now reports it as CLOSED,
        the order should be marked as filled and removed from open orders."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        local_order = _make_exchange_order(
            identifier="fill-1", status=OrderStatus.OPEN, side=OrderSide.BUY, price=2900.0
        )
        mock_order_book.get_open_orders.return_value = [local_order]

        exchange_order = _make_exchange_order(
            identifier="fill-1",
            status=OrderStatus.CLOSED,
            side=OrderSide.BUY,
            price=2900.0,
            filled=1.0,
            remaining=0.0,
            average=2900.0,
            cost=2900.0,
        )
        service.order_execution_strategy.get_order = AsyncMock(return_value=exchange_order)

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.orders_reconciled >= 1
        assert result.ghost_orders_found == 0

        # The local order should have been updated
        assert local_order.status == OrderStatus.CLOSED
        assert local_order.filled == 1.0
        assert local_order.remaining == 0.0
        mock_order_book.remove_open_order.assert_called_once_with(local_order)

    # 5. Order canceled while bot was down
    async def test_order_canceled_while_bot_down(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_order_book,
        mock_balance_tracker,
    ):
        """If an order was open in the DB but the exchange now reports CANCELED,
        the order should be canceled locally and reserved funds released."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        local_order = _make_exchange_order(
            identifier="cancel-1", status=OrderStatus.OPEN, side=OrderSide.BUY, price=2950.0, remaining=1.0
        )
        mock_order_book.get_open_orders.return_value = [local_order]

        exchange_order = _make_exchange_order(
            identifier="cancel-1", status=OrderStatus.CANCELED, side=OrderSide.BUY, price=2950.0
        )
        service.order_execution_strategy.get_order = AsyncMock(return_value=exchange_order)

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.orders_reconciled >= 1
        mock_order_book.update_order_status.assert_any_call("cancel-1", OrderStatus.CANCELED)

        # Reserved fiat should be released: price * remaining = 2950.0 * 1.0
        mock_balance_tracker.release_reserved_fiat.assert_awaited_once_with(2950.0 * 1.0)

    # 6. Ghost order (not found on exchange)
    async def test_ghost_order_not_on_exchange(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_order_book,
        mock_balance_tracker,
    ):
        """If an order exists locally but get_order returns None (not on exchange),
        it should be marked CANCELED as a ghost order with funds released."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        local_order = _make_exchange_order(
            identifier="ghost-1", status=OrderStatus.OPEN, side=OrderSide.SELL, price=3050.0, remaining=0.5
        )
        mock_order_book.get_open_orders.return_value = [local_order]

        service.order_execution_strategy.get_order = AsyncMock(return_value=None)

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.ghost_orders_found == 1
        assert result.orders_reconciled >= 1
        mock_order_book.update_order_status.assert_any_call("ghost-1", OrderStatus.CANCELED)

        # Sell order: reserved crypto should be released with remaining quantity
        mock_balance_tracker.release_reserved_crypto.assert_awaited_once_with(0.5)

    # 7. Orphan order on exchange not in local book
    async def test_orphan_order_on_exchange(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_order_book,
        mock_exchange_service,
    ):
        """If the exchange has open orders that are not tracked locally,
        they should be counted as orphans but not auto-adopted.

        Note: the orphan check only runs when there are local open orders
        (if get_open_orders is empty, _reconcile_with_exchange returns early).
        So we include one known local order that the exchange confirms as OPEN,
        plus one orphan that only exists on the exchange.
        """
        _set_valid_bot_state(mock_repository, mock_config_manager)

        # One known local open order
        known_order = _make_exchange_order(identifier="known-1", status=OrderStatus.OPEN)
        mock_order_book.get_open_orders.return_value = [known_order]

        # Exchange confirms the known order is OPEN and also has an unknown orphan
        exchange_known = _make_exchange_order(identifier="known-1", status=OrderStatus.OPEN)
        service.order_execution_strategy.get_order = AsyncMock(return_value=exchange_known)

        mock_exchange_service.fetch_open_orders = AsyncMock(
            return_value=[
                {"id": "known-1", "side": "buy", "price": 2900.0, "amount": 1.0},
                {"id": "orphan-1", "side": "buy", "price": 2800.0, "amount": 2.0},
            ]
        )

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.orphan_orders_found == 1

    # 8. Balance restored from exchange
    async def test_balance_restored_from_exchange(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_balance_tracker,
        mock_exchange_service,
    ):
        """When the exchange balance fetch succeeds, balance_source should be 'exchange'
        and the balance tracker should reflect exchange values overlaid with DB reserved amounts."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        mock_repository.load_balance_state.return_value = _make_saved_balance(
            fiat="4500", crypto="2.0", fees="15", reserved_fiat="500", reserved_crypto="0.5"
        )

        mock_exchange_service.get_balance = AsyncMock(return_value={"free": {"USDT": 6000, "ETH": 3.0}})

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.balance_source == "exchange"

        # Balance should use exchange free minus DB reserved
        assert mock_balance_tracker._balance == Decimal("5500")  # 6000 - 500
        assert mock_balance_tracker._crypto_balance == Decimal("2.5")  # 3.0 - 0.5
        assert mock_balance_tracker._reserved_fiat == Decimal("500")
        assert mock_balance_tracker._reserved_crypto == Decimal("0.5")
        assert mock_balance_tracker._total_fees == Decimal("15")

    # 9. Balance fallback to DB when exchange fails
    async def test_balance_fallback_to_db(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_balance_tracker,
        mock_exchange_service,
    ):
        """When the exchange balance fetch fails, the service should fall back to DB values
        and report balance_source='db'."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        mock_repository.load_balance_state.return_value = _make_saved_balance(
            fiat="4500", crypto="2.0", fees="15", reserved_fiat="300", reserved_crypto="0.3"
        )

        mock_exchange_service.get_balance = AsyncMock(side_effect=Exception("Exchange unavailable"))

        result = await service.attempt_recovery()

        assert result.recovered is True
        assert result.balance_source == "db"

        assert mock_balance_tracker._balance == Decimal("4500")
        assert mock_balance_tracker._crypto_balance == Decimal("2.0")
        assert mock_balance_tracker._total_fees == Decimal("15")
        assert mock_balance_tracker._reserved_fiat == Decimal("300")
        assert mock_balance_tracker._reserved_crypto == Decimal("0.3")

    # 10. Grid level paired references restored
    async def test_grid_level_paired_references_restored(
        self,
        service,
        mock_repository,
        mock_config_manager,
        mock_grid_manager,
    ):
        """When saved grid levels have paired_buy/sell prices, the paired references
        should be re-linked between the corresponding GridLevel objects."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        gl_low = GridLevel(2900.0, GridCycleState.READY_TO_BUY)
        gl_high = GridLevel(3000.0, GridCycleState.READY_TO_SELL)
        mock_grid_manager.grid_levels = {2900.0: gl_low, 3000.0: gl_high}

        mock_repository.load_grid_levels.return_value = [
            {
                "price": 2900.0,
                "state": "waiting_for_buy_fill",
                "paired_buy_level_price": None,
                "paired_sell_level_price": 3000.0,
            },
            {
                "price": 3000.0,
                "state": "waiting_for_sell_fill",
                "paired_buy_level_price": 2900.0,
                "paired_sell_level_price": None,
            },
        ]

        result = await service.attempt_recovery()

        assert result.recovered is True

        # Check states were restored
        assert gl_low.state == GridCycleState.WAITING_FOR_BUY_FILL
        assert gl_high.state == GridCycleState.WAITING_FOR_SELL_FILL

        # Check paired references
        assert gl_low.paired_sell_level is gl_high
        assert gl_low.paired_buy_level is None
        assert gl_high.paired_buy_level is gl_low
        assert gl_high.paired_sell_level is None

    # 11. Exception during recovery falls back to fresh start
    async def test_recovery_exception_falls_back_to_fresh(
        self,
        service,
        mock_repository,
        mock_config_manager,
    ):
        """If an unexpected exception occurs during recovery, the service should
        call clear_all and return recovered=False with the error message."""
        _set_valid_bot_state(mock_repository, mock_config_manager)

        # Force an exception during grid level restoration
        mock_repository.load_grid_levels.side_effect = RuntimeError("Corrupt DB")

        result = await service.attempt_recovery()

        assert result.recovered is False
        assert len(result.errors) == 1
        assert "Corrupt DB" in result.errors[0]
        mock_repository.clear_all.assert_called_once()
