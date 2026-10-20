from unittest.mock import AsyncMock, Mock, patch

import pytest

from grid_trading_bot.core.order_handling.order import OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.services.exceptions import (
    DataFetchError,
    OrderCancellationError,
    OrderEndedUnfilledError,
    OrderNotFoundError,
    OrderNotPlacedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    PostOnlyRejectedError,
)

CLIENT_ORDER_ID = "g3f2a9c1d0123456789abcdef"


def _raw(
    identifier="order-1",
    status="open",
    order_type="limit",
    side="buy",
    price=100.0,
    amount=1.0,
    filled=0.0,
    symbol="BTC/USDT",
    **extra,
):
    return {
        "id": identifier,
        "status": status,
        "type": order_type,
        "side": side,
        "price": price,
        "amount": amount,
        "filled": filled,
        "remaining": amount - filled,
        "symbol": symbol,
        **extra,
    }


@pytest.mark.asyncio
class TestLiveOrderExecutionStrategy:
    @patch("time.time", return_value=1680000000)
    async def test_execute_market_order_success(self, mock_time, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        raw_order = _raw(
            "test-order-id", "closed", "market", "buy", 30000, 0.5, 0.5, symbol="BTC/USDT", timestamp=1680000000000
        )
        exchange_service.place_order = AsyncMock(return_value=raw_order)

        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 0.5, 30000, CLIENT_ORDER_ID)

        assert order.identifier == "test-order-id"
        assert order.status == OrderStatus.CLOSED
        assert order.order_type == OrderType.MARKET
        assert order.side == OrderSide.BUY
        assert order.price == 30000
        assert (order.filled, order.remaining) == (0.5, 0.0)
        exchange_service.place_order.assert_awaited_once_with(
            "BTC/USDT", "market", "buy", 0.5, 30000, client_order_id=CLIENT_ORDER_ID
        )

    async def test_a_market_order_is_sent_once(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(side_effect=OrderNotPlacedError("Circuit breaker open"))
        exchange_service.fetch_order_by_client_id = AsyncMock()

        with pytest.raises(OrderNotPlacedError):
            await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 0.5, 30000, CLIENT_ORDER_ID)

        assert exchange_service.place_order.await_count == 1
        exchange_service.fetch_order_by_client_id.assert_not_awaited()

    async def test_execute_limit_order_success(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(
            return_value=_raw("test-limit-order-id", "open", "limit", "sell", 2000, 1, 0, symbol="ETH/USDT")
        )

        order = await strategy.execute_limit_order(OrderSide.SELL, "ETH/USDT", 1, 2000, CLIENT_ORDER_ID)

        assert order.identifier == "test-limit-order-id"
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT
        assert order.side == OrderSide.SELL
        assert order.price == 2000
        exchange_service.place_order.assert_awaited_once_with(
            "ETH/USDT", "limit", "sell", 1, 2000, client_order_id=CLIENT_ORDER_ID
        )

    @pytest.mark.parametrize(
        "error",
        [
            OrderNotPlacedError("Circuit breaker open"),
            OrderRejectedError("insufficient funds"),
            PostOnlyRejectedError("would have crossed"),
        ],
    )
    async def test_answers_that_settle_the_outcome_are_not_looked_up(self, setup_live_strategy, error):
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(side_effect=error)
        exchange_service.fetch_order_by_client_id = AsyncMock()

        with pytest.raises(type(error)):
            await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        exchange_service.fetch_order_by_client_id.assert_not_awaited()

    async def test_a_lost_answer_is_resolved_by_the_client_order_id(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.LOOKUP_DELAY = 0
        exchange_service.place_order = AsyncMock(side_effect=OrderOutcomeUnknownError("timed out"))
        exchange_service.fetch_order_by_client_id = AsyncMock(return_value=_raw("found-1"))

        order = await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.identifier == "found-1"
        exchange_service.fetch_order_by_client_id.assert_awaited_once_with(CLIENT_ORDER_ID, "BTC/USDT")

    async def test_an_order_found_on_a_later_lookup_is_returned(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.LOOKUP_DELAY = 0
        exchange_service.place_order = AsyncMock(side_effect=OrderOutcomeUnknownError("timed out"))
        exchange_service.fetch_order_by_client_id = AsyncMock(
            side_effect=[OrderNotFoundError("not yet"), _raw("found-2")]
        )

        order = await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.identifier == "found-2"
        assert exchange_service.fetch_order_by_client_id.await_count == 2

    async def test_an_order_absent_from_every_lookup_was_not_placed(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.LOOKUP_DELAY = 0
        exchange_service.place_order = AsyncMock(side_effect=OrderOutcomeUnknownError("timed out"))
        exchange_service.fetch_order_by_client_id = AsyncMock(side_effect=OrderNotFoundError("unknown"))

        with pytest.raises(OrderNotPlacedError):
            await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert exchange_service.fetch_order_by_client_id.await_count == strategy.LOOKUP_ATTEMPTS == 3

    async def test_a_failing_lookup_leaves_the_outcome_unknown(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.LOOKUP_DELAY = 0
        exchange_service.place_order = AsyncMock(side_effect=OrderOutcomeUnknownError("timed out"))
        exchange_service.fetch_order_by_client_id = AsyncMock(
            side_effect=[OrderNotFoundError("unknown"), DataFetchError("down"), OrderNotFoundError("unknown")]
        )

        with pytest.raises(OrderOutcomeUnknownError):
            await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

    async def test_an_unreadable_answer_is_resolved_by_the_client_order_id(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.LOOKUP_DELAY = 0
        exchange_service.place_order = AsyncMock(return_value={"id": "half-answer"})
        exchange_service.fetch_order_by_client_id = AsyncMock(return_value=_raw("half-answer"))

        order = await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.status == OrderStatus.OPEN

    async def test_a_market_order_whose_answer_was_lost_is_returned_closed(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.LOOKUP_DELAY = 0
        exchange_service.place_order = AsyncMock(side_effect=OrderOutcomeUnknownError("timed out"))
        exchange_service.fetch_order_by_client_id = AsyncMock(
            return_value=_raw("m-1", "closed", "market", "buy", 100.0, 1.0, 1.0, average=101.0)
        )

        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert (order.status, order.filled, order.average) == (OrderStatus.CLOSED, 1.0, 101.0)
        assert exchange_service.place_order.await_count == 1

    async def test_find_order_returns_none_for_an_unknown_client_order_id(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.fetch_order_by_client_id = AsyncMock(side_effect=OrderNotFoundError("unknown"))

        assert await strategy.find_order(CLIENT_ORDER_ID, "BTC/USDT") is None

    async def test_find_order_parses_the_order_it_finds(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.fetch_order_by_client_id = AsyncMock(return_value=_raw("found-3", "closed", filled=1.0))

        order = await strategy.find_order(CLIENT_ORDER_ID, "BTC/USDT")

        assert (order.identifier, order.status) == ("found-3", OrderStatus.CLOSED)

    async def test_find_order_lets_a_failed_lookup_through(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.fetch_order_by_client_id = AsyncMock(side_effect=DataFetchError("down"))

        with pytest.raises(DataFetchError):
            await strategy.find_order(CLIENT_ORDER_ID, "BTC/USDT")

    async def test_rounding_follows_the_exchange(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.round_amount = Mock(return_value=0.123)
        exchange_service.round_price = Mock(return_value=100.45)

        assert strategy.round_amount("BTC/USDT", 0.12345) == 0.123
        assert strategy.round_price("BTC/USDT", 100.4567) == 100.45
        exchange_service.round_amount.assert_called_once_with("BTC/USDT", 0.12345)
        exchange_service.round_price.assert_called_once_with("BTC/USDT", 100.4567)

    async def test_get_order_success(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.fetch_order = AsyncMock(return_value=_raw("test-order-id"))

        order = await strategy.get_order("test-order-id", "BTC/USDT")

        assert order.identifier == "test-order-id"
        assert order.symbol == "BTC/USDT"
        assert order.status == OrderStatus.OPEN
        assert order.order_type == OrderType.LIMIT

    async def test_get_order_data_fetch_error(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.fetch_order = AsyncMock(side_effect=DataFetchError("Order not found"))

        with pytest.raises(DataFetchError):
            await strategy.get_order("test-order-id", "BTC/USDT")

    async def test_handle_partial_fill_cancel_succeeds(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        partial_order = Mock(identifier="partial-order", filled=0.5)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})
        exchange_service.fetch_order = AsyncMock(
            return_value=_raw("partial-order", "canceled", "market", "buy", 100.0, 1.0, 0.5)
        )

        result = await strategy._handle_partial_fill(partial_order, "BTC/USDT")

        assert result.identifier == "partial-order"
        exchange_service.cancel_order.assert_called_once_with("partial-order", "BTC/USDT")

    async def test_handle_partial_fill_cancel_fails(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        partial_order = Mock(identifier="partial-order", filled=0.5)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "failed"})

        result = await strategy._handle_partial_fill(partial_order, "BTC/USDT")

        assert result is None

    async def test_handle_partial_fill_refetch_failure_falls_back_to_placement_order(self, setup_live_strategy):
        """If cancel succeeds but the refetch fails, the placement-response order is used as-is."""
        strategy, exchange_service = setup_live_strategy
        partial_order = Mock(identifier="partial-order", filled=0.5)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})
        exchange_service.fetch_order = AsyncMock(side_effect=DataFetchError("boom"))

        result = await strategy._handle_partial_fill(partial_order, "BTC/USDT")

        assert result is partial_order

    async def test_a_partly_filled_market_leg_is_cancelled_and_returned_closed(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        partial_raw = _raw("partial-order", "open", "market", "buy", 100.0, 1.0, 0.3)
        exchange_service.place_order = AsyncMock(return_value=partial_raw)
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})
        exchange_service.fetch_order = AsyncMock(
            return_value=_raw("partial-order", "canceled", "market", "buy", 100.0, 1.0, 0.3)
        )

        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.status == OrderStatus.CLOSED
        assert order.filled == pytest.approx(0.3)
        assert order.amount == pytest.approx(1.0)
        assert order.remaining == pytest.approx(0.7)
        assert order.average == pytest.approx(100.0)
        assert exchange_service.place_order.await_count == 1

    @pytest.mark.parametrize("filled", [0.3, 0.0])
    async def test_a_market_leg_left_open_by_a_failed_cancel_has_an_unknown_outcome(self, setup_live_strategy, filled):
        """While the leg may still rest or fill, neither a resend nor a final fill is safe to report."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        exchange_service.place_order = AsyncMock(
            return_value=_raw("open-leg", "open", "market", "buy", 30000, 1.0, filled)
        )
        exchange_service.cancel_order = AsyncMock(return_value={"status": "failed"})

        with pytest.raises(OrderOutcomeUnknownError, match="open-leg is still open on the exchange"):
            await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 30000, CLIENT_ORDER_ID)

        assert exchange_service.place_order.await_count == 1
        assert exchange_service.cancel_order.await_count == strategy.max_retries

    @pytest.mark.parametrize(
        ("status", "raised"),
        [("rejected", OrderRejectedError), ("canceled", OrderEndedUnfilledError), ("expired", OrderEndedUnfilledError)],
    )
    async def test_a_market_leg_that_executed_nothing_raises(self, setup_live_strategy, status, raised):
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(return_value=_raw("empty-leg", status, "market", "buy", 100.0, 1.0))

        with pytest.raises(raised):
            await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert exchange_service.place_order.await_count == 1

    async def test_leg_with_no_average_or_price_falls_back_to_attempt_price(self, setup_live_strategy):
        """A leg reporting neither average nor price must not crash; it is booked at the reference price."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        exchange_service.place_order = AsyncMock(
            return_value=_raw("partial-order", "open", "market", "buy", None, 1.0, 0.4)
        )
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})
        exchange_service.fetch_order = AsyncMock(
            return_value=_raw("partial-order", "canceled", "market", "buy", None, 1.0, 0.4)
        )

        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.filled == pytest.approx(0.4)
        assert order.average == pytest.approx(100.0)

    @pytest.mark.parametrize("refetch_error", [DataFetchError("timed out"), OrderNotFoundError("archived")])
    @pytest.mark.parametrize("filled", [0.0, 0.3])
    async def test_a_cancelled_market_leg_whose_final_state_cannot_be_read_has_an_unknown_outcome(
        self, setup_live_strategy, refetch_error, filled
    ):
        """A fill that landed between the placement answer and the cancel would go unseen."""
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(
            return_value=_raw("open-leg", "open", "market", "buy", 100.0, 1.0, filled)
        )
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})
        exchange_service.fetch_order = AsyncMock(side_effect=refetch_error)

        with pytest.raises(
            OrderOutcomeUnknownError, match="open-leg was cancelled but its final state could not be read"
        ):
            await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert exchange_service.place_order.await_count == 1

    async def test_cancel_window_fill_uses_refetched_amount(self, setup_live_strategy):
        """Fills landing between the placement response and the cancel must be captured via refetch."""
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        exchange_service.place_order = AsyncMock(
            return_value=_raw("partial-order", "open", "market", "buy", 100.0, 1.0, 0.2)
        )
        exchange_service.cancel_order = AsyncMock(return_value={"status": "canceled"})
        exchange_service.fetch_order = AsyncMock(
            return_value=_raw("partial-order", "canceled", "market", "buy", 100.0, 1.0, 0.35, average=101.0)
        )

        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.filled == pytest.approx(0.35)
        assert order.average == pytest.approx(101.0)
        assert order.remaining == pytest.approx(0.65)

    async def test_an_expired_leg_returns_what_it_filled(self, setup_live_strategy):
        """An expired leg that reports a partial fill must be accounted, not dropped."""
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(
            return_value=_raw("expired-order", "expired", "market", "buy", 100.0, 1.0, 0.3)
        )

        order = await strategy.execute_market_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert order.status == OrderStatus.CLOSED
        assert order.filled == pytest.approx(0.3)
        assert order.remaining == pytest.approx(0.7)
        assert exchange_service.place_order.await_count == 1

    async def test_retry_cancel_order(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.cancel_order = AsyncMock(side_effect=[{"status": "failed"}, {"status": "canceled"}])

        result = await strategy._retry_cancel_order("test-order-id", "BTC/USDT")

        assert result is True
        assert exchange_service.cancel_order.call_count == 2

    async def test_retry_cancel_order_status_closed_is_success(self, setup_live_strategy):
        """A "closed" cancellation result (order already filled by the time cancel landed) is a success."""
        strategy, exchange_service = setup_live_strategy
        exchange_service.cancel_order = AsyncMock(return_value={"status": "closed"})

        result = await strategy._retry_cancel_order("order-id", "BTC/USDT")

        assert result is True
        exchange_service.cancel_order.assert_called_once()

    async def test_retry_cancel_order_treats_order_not_found_as_success(self, setup_live_strategy):
        """An order the exchange no longer knows is already gone, which is what the cancellation wanted."""
        strategy, exchange_service = setup_live_strategy
        exchange_service.cancel_order = AsyncMock(side_effect=OrderNotFoundError("Order abc not found"))

        result = await strategy._retry_cancel_order("abc", "BTC/USDT")

        assert result is True
        exchange_service.cancel_order.assert_called_once()

    async def test_a_cancellation_error_mentioning_not_found_is_a_failed_attempt(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        strategy.retry_delay = 0
        exchange_service.cancel_order = AsyncMock(
            side_effect=OrderCancellationError("Order abc not found for cancellation. It may already be completed.")
        )

        result = await strategy._retry_cancel_order("abc", "BTC/USDT")

        assert result is False
        assert exchange_service.cancel_order.call_count == strategy.max_retries

    async def test_cancel_order_bulk_all_processed_when_middle_order_not_found(self, setup_live_strategy):
        """A stale (already-gone) order among a batch must not abort processing of the others."""
        strategy, exchange_service = setup_live_strategy

        async def fake_cancel(order_id, _pair):
            if order_id == "order-2":
                raise OrderNotFoundError(f"Order {order_id} not found")
            return {"status": "canceled"}

        exchange_service.cancel_order = AsyncMock(side_effect=fake_cancel)

        results = [await strategy.cancel_order(order_id, "BTC/USDT") for order_id in ("order-1", "order-2", "order-3")]

        assert results == [True, True, True]
        assert exchange_service.cancel_order.call_count == 3


@pytest.mark.asyncio
class TestLiveOrderExecutionStrategyPostOnlyRejection:
    """
    Regression guard: PostOnlyRejectedError must reach the OrderManager intact, never handled as a transient
    exchange failure.
    """

    async def test_rejection_reaches_the_caller_unwrapped(self, setup_live_strategy):
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(side_effect=PostOnlyRejectedError("would have crossed"))

        with pytest.raises(PostOnlyRejectedError):
            await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

    async def test_rejection_is_not_retried(self, setup_live_strategy):
        """The price is a grid level; retrying the identical order would just be rejected again."""
        strategy, exchange_service = setup_live_strategy
        exchange_service.place_order = AsyncMock(side_effect=PostOnlyRejectedError("would have crossed"))

        with pytest.raises(PostOnlyRejectedError):
            await strategy.execute_limit_order(OrderSide.BUY, "BTC/USDT", 1.0, 100.0, CLIENT_ORDER_ID)

        assert exchange_service.place_order.await_count == 1


class TestParsedFees:
    async def test_a_fee_list_is_kept_when_the_single_fee_is_empty(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        fees = [{"cost": 0.001, "currency": "BTC"}, {"cost": 0.002, "currency": "BTC"}]
        raw_order = {"id": "1", "status": "closed", "type": "market", "side": "buy", "fee": None, "fees": fees}

        order = await strategy._parse_order_result(raw_order)

        assert order.fee == fees

    async def test_a_fee_list_is_preferred_to_a_single_fee_without_a_cost(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        fees = [{"cost": 0.001, "currency": "BTC"}]
        raw_order = {
            "id": "1",
            "status": "closed",
            "type": "market",
            "side": "buy",
            "fee": {"cost": None, "currency": None},
            "fees": fees,
        }

        order = await strategy._parse_order_result(raw_order)

        assert order.fee == fees

    async def test_a_single_fee_with_a_cost_is_kept(self, setup_live_strategy):
        strategy, _ = setup_live_strategy
        fee = {"cost": 0.003, "currency": "BTC"}
        raw_order = {"id": "1", "status": "closed", "type": "market", "side": "buy", "fee": fee, "fees": [fee, fee]}

        order = await strategy._parse_order_result(raw_order)

        assert order.fee == fee
