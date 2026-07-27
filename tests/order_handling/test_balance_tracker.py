from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType
from grid_trading_bot.core.validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
)


def _make_order(
    side: OrderSide,
    price: float = 100.0,
    average: float | None = None,
    amount: float = 1.0,
    filled: float = 1.0,
    remaining: float = 0.0,
    order_type: OrderType = OrderType.LIMIT,
    status: OrderStatus = OrderStatus.OPEN,
) -> Order:
    return Order(
        identifier="order-1",
        status=status,
        order_type=order_type,
        side=side,
        price=price,
        average=average,
        amount=amount,
        filled=filled,
        remaining=remaining,
        timestamp=0,
        datetime=None,
        last_trade_timestamp=None,
        symbol="BTC/USDT",
        time_in_force=None,
    )


class TestBalanceTracker:
    @pytest.fixture
    def setup_balance_tracker(self):
        event_bus = Mock(spec=EventBus)
        fee_calculator = Mock(spec=FeeCalculator)
        balance_tracker = BalanceTracker(
            event_bus=event_bus,
            fee_calculator=fee_calculator,
            trading_mode=TradingMode.LIVE,
            base_currency="BTC",
            quote_currency="USDT",
        )
        return balance_tracker, fee_calculator, event_bus

    def test_initialization(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        assert balance_tracker.balance == 0
        assert balance_tracker.crypto_balance == 0
        assert balance_tracker.total_fees == 0
        assert balance_tracker.reserved_fiat == 0
        assert balance_tracker.reserved_crypto == 0

    @pytest.mark.asyncio
    async def test_reserve_funds_for_buy(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")

        await balance_tracker.reserve_funds_for_buy(200)

        assert balance_tracker.reserved_fiat == 200
        assert balance_tracker.balance == 800

    @pytest.mark.asyncio
    async def test_reserve_funds_for_buy_insufficient_balance(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        with pytest.raises(InsufficientBalanceError):
            await balance_tracker.reserve_funds_for_buy(1200)

    @pytest.mark.asyncio
    async def test_reserve_funds_for_sell(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("5")

        await balance_tracker.reserve_funds_for_sell(2)

        assert balance_tracker.reserved_crypto == 2
        assert balance_tracker.crypto_balance == 3

    @pytest.mark.asyncio
    async def test_reserve_funds_for_sell_insufficient_balance(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        with pytest.raises(InsufficientCryptoBalanceError):
            await balance_tracker.reserve_funds_for_sell(10)

    @pytest.mark.asyncio
    async def test_get_adjusted_fiat_balance(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")

        await balance_tracker.reserve_funds_for_buy(200)

        assert balance_tracker.get_adjusted_fiat_balance() == 1000

    @pytest.mark.asyncio
    async def test_get_adjusted_crypto_balance(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("5")

        await balance_tracker.reserve_funds_for_sell(2)

        assert balance_tracker.get_adjusted_crypto_balance() == 5

    def test_update_after_buy_order_filled(self, setup_balance_tracker):
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("5")
        fee_calculator.calculate_fee.return_value = 10
        balance_tracker._reserved_fiat = Decimal("500")

        balance_tracker._update_after_buy_order_filled(quantity=1, price=100)

        assert balance_tracker.crypto_balance == 6
        assert balance_tracker.total_fees == 10
        assert balance_tracker.reserved_fiat == 390

    def test_update_after_sell_order_filled(self, setup_balance_tracker):
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        fee_calculator.calculate_fee.return_value = 10
        balance_tracker._reserved_crypto = Decimal("2")

        balance_tracker._update_after_sell_order_filled(quantity=1, price=200)

        assert balance_tracker.balance == 1190
        assert balance_tracker.total_fees == 10
        assert balance_tracker.reserved_crypto == 1

    @pytest.mark.asyncio
    async def test_update_balance_on_order_completion(self, setup_balance_tracker):
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        balance_tracker._crypto_balance = Decimal("5")
        fee_calculator.calculate_fee.return_value = 5  # Mock fee calculation

        buy_order = Mock(side=OrderSide.BUY, filled=1, price=100, average=None)
        balance_tracker._reserved_fiat = Decimal("105")  # Reserved fiat for the buy order (price + fee)
        await balance_tracker._update_balance_on_order_completion(buy_order)
        assert balance_tracker.crypto_balance == 6  # Crypto balance increases by 1
        assert balance_tracker.total_fees == 5  # Total fees reflect the buy order fee
        assert balance_tracker.reserved_fiat == 0  # Reserved fiat should be fully consumed

        sell_order = Mock(side=OrderSide.SELL, filled=1, price=200, average=None)
        balance_tracker._reserved_crypto = Decimal("1")  # Reserved crypto for the sell order
        await balance_tracker._update_balance_on_order_completion(sell_order)
        assert balance_tracker.total_fees == 10  # Total fees include the sell order fee
        assert balance_tracker.reserved_crypto == 0  # Reserved crypto should be fully consumed
        assert balance_tracker.balance == 1195  # Remaining balance after the sell order

    @pytest.mark.asyncio
    async def test_update_balance_on_order_completion_uses_average_fill_price(self, setup_balance_tracker):
        """Slipped fills must hit the balances at the actual fill price (order.average),
        not the requested price — otherwise backtest_slippage has zero P&L effect."""
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        balance_tracker._crypto_balance = Decimal("5")
        fee_calculator.calculate_fee.return_value = 0

        buy_order = Mock(side=OrderSide.BUY, filled=1, price=100, average=101.0)
        balance_tracker._reserved_fiat = Decimal("101")
        await balance_tracker._update_balance_on_order_completion(buy_order)
        assert balance_tracker.reserved_fiat == 0  # 101 consumed at the slipped price, not 100
        assert balance_tracker.crypto_balance == 6

        sell_order = Mock(side=OrderSide.SELL, filled=1, price=200, average=198.0)
        balance_tracker._reserved_crypto = Decimal("1")
        await balance_tracker._update_balance_on_order_completion(sell_order)
        assert balance_tracker.balance == 1198  # proceeds at 198, not 200
        assert balance_tracker.reserved_crypto == 0

    def test_get_total_balance_value(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        balance_tracker._crypto_balance = Decimal("5")
        assert balance_tracker.get_total_balance_value(price=200) == 2000

    def test_event_subscription(self, setup_balance_tracker):
        balance_tracker, _, event_bus = setup_balance_tracker
        event_bus.subscribe.assert_called_once_with(
            Events.ORDER_FILLED,
            balance_tracker._update_balance_on_order_completion,
        )

    @pytest.mark.asyncio
    async def test_setup_balances_backtest(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker.trading_mode = TradingMode.BACKTEST

        await balance_tracker.setup_balances(initial_balance=2000, initial_crypto_balance=10)

        assert balance_tracker.balance == 2000
        assert balance_tracker.crypto_balance == 10

    @pytest.mark.asyncio
    async def test_setup_balances_live_mode(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        mock_exchange_service = AsyncMock()
        balance_tracker._fetch_live_balances = AsyncMock(
            return_value=(Decimal("1500"), Decimal("5")),
        )

        balance_tracker.trading_mode = TradingMode.LIVE
        await balance_tracker.setup_balances(
            initial_balance=0,
            initial_crypto_balance=0,
            exchange_service=mock_exchange_service,
        )

        balance_tracker._fetch_live_balances.assert_awaited_once_with(mock_exchange_service)
        assert balance_tracker.balance == 1500
        assert balance_tracker.crypto_balance == 5

    @pytest.mark.asyncio
    async def test_setup_balances_paper_trading_mode(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker

        balance_tracker.trading_mode = TradingMode.PAPER_TRADING
        await balance_tracker.setup_balances(
            initial_balance=2000,
            initial_crypto_balance=10,
        )

        assert balance_tracker.balance == 2000
        assert balance_tracker.crypto_balance == 10

    @pytest.mark.asyncio
    async def test_fetch_live_balances_success(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        mock_exchange_service = AsyncMock()
        mock_exchange_service.get_balance.return_value = {
            "free": {
                "USDT": 1000,
                "BTC": 5,
            },
        }

        balances = await balance_tracker._fetch_live_balances(exchange_service=mock_exchange_service)

        mock_exchange_service.get_balance.assert_awaited_once()
        assert balances == (Decimal("1000"), Decimal("5"))

    @pytest.mark.asyncio
    async def test_fetch_live_balances_unexpected_structure(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        mock_exchange_service = AsyncMock()
        mock_exchange_service.get_balance.return_value = None

        with pytest.raises(ValueError, match="Unexpected balance structure: None"):
            await balance_tracker._fetch_live_balances(exchange_service=mock_exchange_service)

    # ── release_reserved_fiat ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_reserved_fiat_full(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("800")
        balance_tracker._reserved_fiat = Decimal("200")

        await balance_tracker.release_reserved_fiat(200)

        assert balance_tracker.balance == 1000
        assert balance_tracker.reserved_fiat == 0

    @pytest.mark.asyncio
    async def test_release_reserved_fiat_partial(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("800")
        balance_tracker._reserved_fiat = Decimal("200")

        await balance_tracker.release_reserved_fiat(100)

        assert balance_tracker.balance == 900
        assert balance_tracker.reserved_fiat == 100

    @pytest.mark.asyncio
    async def test_release_reserved_fiat_over_release(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("800")
        balance_tracker._reserved_fiat = Decimal("200")

        await balance_tracker.release_reserved_fiat(500)

        assert balance_tracker.balance == 1000
        assert balance_tracker.reserved_fiat == 0

    # ── release_reserved_crypto ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_reserved_crypto_full(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("3")
        balance_tracker._reserved_crypto = Decimal("2")

        await balance_tracker.release_reserved_crypto(2)

        assert balance_tracker.crypto_balance == 5
        assert balance_tracker.reserved_crypto == 0

    @pytest.mark.asyncio
    async def test_release_reserved_crypto_partial(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("3")
        balance_tracker._reserved_crypto = Decimal("2")

        await balance_tracker.release_reserved_crypto(1)

        assert balance_tracker.crypto_balance == 4
        assert balance_tracker.reserved_crypto == 1

    @pytest.mark.asyncio
    async def test_release_reserved_crypto_over_release(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("3")
        balance_tracker._reserved_crypto = Decimal("2")

        await balance_tracker.release_reserved_crypto(10)

        assert balance_tracker.crypto_balance == 5
        assert balance_tracker.reserved_crypto == 0


@pytest.fixture
def balance_tracker():
    event_bus = Mock(spec=EventBus)
    fee_calculator = Mock(spec=FeeCalculator)
    fee_calculator.calculate_fee.side_effect = lambda amount: amount * 0.001
    bt = BalanceTracker(
        event_bus=event_bus,
        fee_calculator=fee_calculator,
        trading_mode=TradingMode.LIVE,
        base_currency="BTC",
        quote_currency="USDT",
    )
    bt._balance = Decimal("1000")
    return bt


@pytest.fixture
def balance_tracker_with_crypto(balance_tracker):
    balance_tracker._crypto_balance = Decimal("2.0")
    balance_tracker._balance = Decimal("0")
    return balance_tracker


class TestSettleCancelledOrder:
    async def test_zero_fill_buy_releases_full_reservation(self, balance_tracker):
        initial_balance = balance_tracker.balance
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(side=OrderSide.BUY, price=100.0, amount=1.0, filled=0.0, remaining=1.0)

        await balance_tracker.settle_cancelled_order(order)

        assert balance_tracker.reserved_fiat == 0.0
        assert balance_tracker.balance == pytest.approx(initial_balance)

    async def test_partial_fill_buy_credits_crypto_and_releases_remainder(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(side=OrderSide.BUY, price=100.0, average=100.0, amount=1.0, filled=0.4, remaining=0.6)

        await balance_tracker.settle_cancelled_order(order)

        assert balance_tracker.crypto_balance == pytest.approx(0.4)
        # reserved consumed for the filled 40 (+fee) and released for the remaining 60
        assert balance_tracker.reserved_fiat == pytest.approx(0.0, abs=1e-6)

    async def test_partial_fill_sell_credits_fiat_and_releases_remainder(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto  # 2.0 crypto, 0 fiat
        await bt.reserve_funds_for_sell(1.0)
        order = _make_order(side=OrderSide.SELL, price=100.0, average=100.0, amount=1.0, filled=0.3, remaining=0.7)

        await bt.settle_cancelled_order(order)

        assert bt.reserved_crypto == pytest.approx(0.0, abs=1e-8)
        assert bt.crypto_balance == pytest.approx(1.7)
        assert bt.balance > 0  # proceeds of 0.3 * 100 minus fee


class TestUpdateAfterLiquidation:
    async def test_liquidation_credits_proceeds_and_debits_crypto(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        order = _make_order(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            price=100.0,
            average=99.0,
            amount=2.0,
            filled=2.0,
            remaining=0.0,
        )

        await bt.update_after_liquidation(order)

        fee = bt.fee_calculator.calculate_fee(2.0 * 99.0)
        assert bt.balance == pytest.approx(2.0 * 99.0 - fee)
        assert bt.crypto_balance == 0.0
