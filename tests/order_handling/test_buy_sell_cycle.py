import pytest
from tests.fakes.trading_stack import ARMING_PRICE, build_stack, reserved_matches_open_orders

from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling.order import OrderSide
from grid_trading_bot.core.order_handling.order_status_tracker import OrderStatusTracker
from grid_trading_bot.core.services.exceptions import DataFetchError


class TestParseOrderResult:
    """LiveOrderExecutionStrategy._parse_order_result on malformed exchange responses."""

    @pytest.mark.parametrize("missing_field", ["id", "status", "type", "side"])
    async def test_missing_required_fields_raises_error(self, setup_live_strategy, missing_field):
        strategy, _ = setup_live_strategy
        raw_order = {
            "id": "order_123",
            "status": "closed",
            "type": "limit",
            "side": "buy",
            "price": 50000,
            "amount": 0.01,
        }
        del raw_order[missing_field]

        with pytest.raises(DataFetchError, match=f"Exchange response missing required fields: {missing_field}"):
            await strategy._parse_order_result(raw_order)

    async def test_unparseable_status_raises_value_error(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        raw_order = {
            "id": "order_123",
            "status": "foobar",
            "type": "limit",
            "side": "buy",
        }

        with pytest.raises(ValueError, match="'foobar' is not a valid OrderStatus"):
            await strategy._parse_order_result(raw_order)


class TestTrackerToOrderManagerFlow:
    """The tracker hands a fill it detects to the order manager, which places the paired sell."""

    async def test_tracker_detects_fill_and_triggers_sell_placement(self):
        stack = build_stack()
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        tracker = OrderStatusTracker(stack.book, stack.execution, stack.manager, polling_interval=1.0)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        stack.execution.fill(buy.identifier)

        await tracker._process_open_orders()

        sell = stack.execution.order_at(110.0, OrderSide.SELL)
        assert sell.amount == pytest.approx(buy.amount * 0.999)
        assert stack.book.get_order(sell.identifier).is_open()
        assert stack.book.get_order(buy.identifier).is_filled()

    async def test_a_partial_fill_waits_and_the_completed_order_pairs_its_whole_amount(self):
        stack = build_stack()
        await stack.manager.initialize_grid_orders(ARMING_PRICE)
        tracker = OrderStatusTracker(stack.book, stack.execution, stack.manager, polling_interval=1.0)
        buy = stack.execution.order_at(100.0, OrderSide.BUY)
        balances = (stack.balances.balance, stack.balances.crypto_balance, stack.balances.reserved_fiat)
        stack.execution.fill(buy.identifier, buy.amount * 0.3)

        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_open()
        assert (stack.balances.balance, stack.balances.crypto_balance, stack.balances.reserved_fiat) == balances
        assert [o.price for o in stack.execution.open_orders(OrderSide.SELL)] == [120.0]
        assert stack.grid.grid_levels[100.0].state == GridCycleState.WAITING_FOR_BUY_FILL

        stack.execution.fill(buy.identifier)
        await tracker._process_open_orders()

        assert stack.book.get_order(buy.identifier).is_filled()
        assert stack.execution.order_at(110.0, OrderSide.SELL).amount == pytest.approx(buy.amount * 0.999)
        assert reserved_matches_open_orders(stack)
