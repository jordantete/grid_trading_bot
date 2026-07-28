from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.core.order_handling.exceptions import OrderExecutionFailedError
from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.services.exceptions import DataFetchError


@pytest.mark.asyncio
class TestLiveOrderExecutionStrategy:
    @patch("time.time", return_value=1680000000)  # Mock time for predictable order IDs
    async def test_execute_market_order_success(self, mock_time, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        pair = "BTC/USDT"
        quantity = 0.5
        price = 30000
        raw_order = {
            "id": "test-order-id",
            "status": "closed",
            "type": "market",
            "side": "buy",
            "price": price,
            "amount": quantity,
            "filled": quantity,
            "remaining": 0,
            "symbol": pair,
            "timestamp": 1680000000000,
        }

        exchange_service.place_order = AsyncMock(return_value=raw_order)

        order = await strategy.execute_market_order(OrderSide.BUY, pair, quantity, price)

        assert order is not None
        assert order.identifier == "test-order-id"
        assert order.status == OrderStatus.CLOSED
        assert order.order_type == OrderType.MARKET
        assert order.side == OrderSide.BUY
        assert order.price == price

    async def test_execute_market_order_retries(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        pair = "BTC/USDT"
        quantity = 0.5
        price = 30000

        exchange_service.place_order = AsyncMock(side_effect=DataFetchError("Order failed"))

        with pytest.raises(OrderExecutionFailedError):
            await strategy.execute_market_order(OrderSide.BUY, pair, quantity, price)

        assert exchange_service.place_order.call_count == strategy.max_retries

    async def test_execute_limit_order_success(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        pair = "ETH/USDT"
        quantity = 1
        price = 2000
        raw_order = {
            "id": "test-limit-order-id",
            "status": "open",
            "type": "limit",
            "side": "sell",
            "price": price,
            "amount": quantity,
            "filled": 0,
            "remaining": quantity,
            "symbol": pair,
        }

        exchange_service.place_order = AsyncMock(return_value=raw_order)

        order = await strategy.execute_limit_order(OrderSide.SELL, pair, quantity, price)

        assert order is not None
        assert order.identifier == "test-limit-order-id"
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT
        assert order.side == OrderSide.SELL
        assert order.price == price

    async def test_execute_limit_order_data_fetch_error(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        pair = "ETH/USDT"
        quantity = 1
        price = 2000

        exchange_service.place_order = AsyncMock(side_effect=DataFetchError("Exchange API error"))

        with pytest.raises(OrderExecutionFailedError):
            await strategy.execute_limit_order(OrderSide.SELL, pair, quantity, price)

    async def test_get_order_success(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        order_id = "test-order-id"
        pair = "BTC/USDT"
        raw_order = {
            "id": order_id,
            "status": "open",
            "type": "limit",
            "side": "buy",
            "price": 100,
            "amount": 1,
            "filled": 0,
            "remaining": 1,
            "symbol": pair,
        }

        exchange_service.fetch_order = AsyncMock(return_value=raw_order)

        order = await strategy.get_order(order_id, pair)

        assert order is not None
        assert order.identifier == order_id
        assert order.symbol == pair
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT

    async def test_get_order_data_fetch_error(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        order_id = "test-order-id"
        pair = "BTC/USDT"

        exchange_service.fetch_order = AsyncMock(side_effect=DataFetchError("Order not found"))

        with pytest.raises(DataFetchError):
            await strategy.get_order(order_id, pair)

    async def test_handle_partial_fill_cancel_succeeds(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        partial_order = Mock(identifier="partial-order", filled=0.5)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})

        result = await strategy._handle_partial_fill(partial_order, "BTC/USDT")

        assert result is True
        exchange_service.cancel_order.assert_called_once_with("partial-order", "BTC/USDT")

    async def test_handle_partial_fill_cancel_fails(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        partial_order = Mock(identifier="partial-order", filled=0.5)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "failed"})

        result = await strategy._handle_partial_fill(partial_order, "BTC/USDT")

        assert result is False

    async def test_execute_market_order_partial_fill_cancel_succeeds_retries_remaining(self, setup_live_strategy):
        """Partial fill → cancel succeeds → retry with remaining quantity → fills are aggregated."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0  # Speed up test
        pair = "BTC/USDT"
        quantity = 1.0
        price = 100.0

        partial_raw = {
            "id": "partial-order",
            "status": "open",
            "type": "market",
            "side": "buy",
            "price": 100.0,
            "amount": quantity,
            "filled": 0.3,
            "remaining": 0.7,
            "symbol": pair,
        }
        closed_raw = {
            "id": "final-order",
            "status": "closed",
            "type": "market",
            "side": "buy",
            "price": 102.0,
            "amount": 0.7,
            "filled": 0.7,
            "remaining": 0,
            "symbol": pair,
        }

        exchange_service.place_order = AsyncMock(side_effect=[partial_raw, closed_raw])
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})

        order = await strategy.execute_market_order(OrderSide.BUY, pair, quantity, price)

        assert order is not None
        assert order.status == OrderStatus.CLOSED
        assert order.filled == pytest.approx(1.0)
        assert order.amount == pytest.approx(1.0)
        assert order.average == pytest.approx((0.3 * 100.0 + 0.7 * 102.0) / 1.0)
        # Second call should use remaining quantity (0.7)
        second_call = exchange_service.place_order.call_args_list[1]
        assert second_call[0][3] == pytest.approx(0.7)

    async def test_execute_market_order_partial_fill_cancel_fails_returns_partial(self, setup_live_strategy):
        """Partial fill → cancel fails → returns partial aggregate (no double-spend)."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        pair = "BTC/USDT"
        quantity = 1.0
        price = 30000

        partial_raw = {
            "id": "partial-order",
            "status": "open",
            "type": "market",
            "side": "buy",
            "price": price,
            "amount": quantity,
            "filled": 0.3,
            "remaining": 0.7,
            "symbol": pair,
        }

        exchange_service.place_order = AsyncMock(return_value=partial_raw)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "failed"})

        order = await strategy.execute_market_order(OrderSide.BUY, pair, quantity, price)

        assert order is not None
        assert order.status == OrderStatus.CLOSED
        assert order.filled == pytest.approx(0.3)
        assert order.amount == pytest.approx(1.0)
        assert order.remaining == pytest.approx(0.7)
        # Should have only placed ONE order (no retry after failed cancel)
        assert exchange_service.place_order.call_count == 1

    async def test_exhaustion_with_partial_fills_returns_aggregate(self, setup_live_strategy):
        """Every leg partially fills and retries are exhausted → aggregate of all legs is returned."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        pair = "SOL/USDT"
        quantity = 1.0
        price = 100.0

        partial_raw = {
            "id": "partial-order",
            "status": "open",
            "type": "market",
            "side": "buy",
            "price": price,
            "amount": quantity,
            "filled": 0.2,
            "remaining": 0.8,
            "symbol": pair,
        }

        exchange_service.place_order = AsyncMock(return_value=partial_raw)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})

        order = await strategy.execute_market_order(OrderSide.BUY, pair, quantity, price)

        assert exchange_service.place_order.call_count == strategy.max_retries
        assert order.filled == pytest.approx(0.6)
        assert order.remaining == pytest.approx(0.4)
        assert order.status == OrderStatus.CLOSED

    async def test_exhaustion_with_zero_fill_raises(self, setup_live_strategy):
        """No leg ever fills anything → raises instead of returning an empty aggregate."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        pair = "SOL/USDT"

        exchange_service.place_order = AsyncMock(side_effect=DataFetchError("Order failed"))

        with pytest.raises(OrderExecutionFailedError):
            await strategy.execute_market_order(OrderSide.BUY, pair, 1.0, 100.0)

    async def test_rejected_leg_retries_full_remaining(self, setup_live_strategy):
        """A rejected leg contributes no fill and must not shrink the quantity of the next retry."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        pair = "SOL/USDT"
        quantity = 1.0
        price = 100.0

        rejected_raw = {
            "id": "rejected-order",
            "status": "rejected",
            "type": "market",
            "side": "buy",
            "price": price,
            "amount": quantity,
            "filled": 0,
            "remaining": quantity,
            "symbol": pair,
        }
        closed_raw = {
            "id": "final-order",
            "status": "closed",
            "type": "market",
            "side": "buy",
            "price": price,
            "amount": quantity,
            "filled": 1.0,
            "remaining": 0,
            "symbol": pair,
        }

        exchange_service.place_order = AsyncMock(side_effect=[rejected_raw, closed_raw])

        order = await strategy.execute_market_order(OrderSide.BUY, pair, quantity, price)

        assert order.filled == pytest.approx(1.0)
        # Second call must still use the full original quantity, not decremented by the rejected leg.
        second_call = exchange_service.place_order.call_args_list[1]
        assert second_call[0][3] == pytest.approx(quantity)

    async def test_retry_cancel_order(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        order_id = "test-order-id"
        pair = "BTC/USDT"

        exchange_service.cancel_order = AsyncMock(
            side_effect=[
                {"status": "failed"},
                {"status": "canceled"},
            ],
        )

        result = await strategy._retry_cancel_order(order_id, pair)

        assert result is True
        assert exchange_service.cancel_order.call_count == 2

    async def test_adjust_price_buy(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        price = 30000
        adjusted_price = await strategy._adjust_price(OrderSide.BUY, price, 1)

        assert adjusted_price > price

    async def test_adjust_price_sell(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        price = 30000
        adjusted_price = await strategy._adjust_price(OrderSide.SELL, price, 1)

        assert adjusted_price < price

    async def test_adjust_price_first_attempt_no_adjustment(self, setup_live_strategy):
        """Attempt 0 should produce no adjustment."""
        strategy, _ = setup_live_strategy
        price = 30000
        adjusted_price = await strategy._adjust_price(OrderSide.BUY, price, 0)

        assert adjusted_price == price
