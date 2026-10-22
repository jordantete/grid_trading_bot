from decimal import Decimal
import logging
import random
from unittest.mock import AsyncMock

import pytest
from tests.fakes.config import config_manager_from

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.fee_calculator import Fee, FeeCalculator
from grid_trading_bot.core.order_handling.order import Liquidity, Order, OrderSide, OrderStatus, OrderType
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
    fee: dict | list | None = None,
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
        fee=fee,
    )


def _fees(valid_config: dict, rate: float = 0.001) -> FeeCalculator:
    config = config_manager_from(
        valid_config,
        {"exchange": {"trading_fee": rate}, "pair": {"base_currency": "BTC", "quote_currency": "USDT"}},
    )
    return FeeCalculator(config)


class TestBalanceTracker:
    @pytest.fixture
    def setup_balance_tracker(self, valid_config):
        fee_calculator = _fees(valid_config)
        balance_tracker = BalanceTracker(
            fee_calculator=fee_calculator,
            trading_mode=TradingMode.LIVE,
            base_currency="BTC",
            quote_currency="USDT",
        )
        return balance_tracker, fee_calculator

    def test_initialization(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        assert balance_tracker.balance == 0
        assert balance_tracker.crypto_balance == 0
        assert balance_tracker.total_fees == 0
        assert balance_tracker.reserved_fiat == 0
        assert balance_tracker.reserved_crypto == 0

    @pytest.mark.asyncio
    async def test_setup_balances_clears_stale_reservations(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker.trading_mode = TradingMode.PAPER_TRADING
        balance_tracker._reserved_fiat = Decimal("50")
        balance_tracker._reserved_crypto = Decimal("1")

        await balance_tracker.setup_balances(initial_balance=1000.0, initial_crypto_balance=0.0)

        assert balance_tracker.reserved_fiat == 0.0
        assert balance_tracker.reserved_crypto == 0.0

    @pytest.mark.asyncio
    async def test_reserve_funds_for_buy(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")

        await balance_tracker.reserve_funds_for_buy(200)

        assert balance_tracker.reserved_fiat == 200
        assert balance_tracker.balance == 800

    @pytest.mark.asyncio
    async def test_reserve_funds_for_buy_insufficient_balance(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        with pytest.raises(InsufficientBalanceError):
            await balance_tracker.reserve_funds_for_buy(1200)

    @pytest.mark.asyncio
    async def test_reserve_funds_for_sell(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("5")

        await balance_tracker.reserve_funds_for_sell(2)

        assert balance_tracker.reserved_crypto == 2
        assert balance_tracker.crypto_balance == 3

    @pytest.mark.asyncio
    async def test_reserve_funds_for_sell_insufficient_balance(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        with pytest.raises(InsufficientCryptoBalanceError):
            await balance_tracker.reserve_funds_for_sell(10)

    @pytest.mark.asyncio
    async def test_get_adjusted_fiat_balance(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")

        await balance_tracker.reserve_funds_for_buy(200)

        assert balance_tracker.get_adjusted_fiat_balance() == 1000

    @pytest.mark.asyncio
    async def test_get_adjusted_crypto_balance(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("5")

        await balance_tracker.reserve_funds_for_sell(2)

        assert balance_tracker.get_adjusted_crypto_balance() == 5

    def test_update_after_buy_order_filled(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("5")
        balance_tracker._reserved_fiat = Decimal("500")

        balance_tracker._update_after_buy_order_filled(quantity=1, price=100, fee=Fee(0.01, in_base=True))

        assert balance_tracker.crypto_balance == pytest.approx(5.99)
        assert balance_tracker.total_fees == pytest.approx(1.0)
        assert balance_tracker.reserved_fiat == 400

    def test_update_after_sell_order_filled(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        balance_tracker._reserved_crypto = Decimal("2")

        balance_tracker._update_after_sell_order_filled(quantity=1, price=200, fee=Fee(10.0, in_base=False))

        assert balance_tracker.balance == 1190
        assert balance_tracker.total_fees == 10
        assert balance_tracker.reserved_crypto == 1

    @pytest.mark.asyncio
    async def test_update_balance_on_order_completion(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        balance_tracker._crypto_balance = Decimal("5")
        balance_tracker._reserved_fiat = Decimal("100")
        buy_order = _make_order(OrderSide.BUY, price=100, status=OrderStatus.CLOSED)

        await balance_tracker.record_execution(buy_order, buy_order.filled)

        assert balance_tracker.crypto_balance == pytest.approx(5.999)
        assert balance_tracker.total_fees == pytest.approx(0.1)
        assert balance_tracker.reserved_fiat == 0

        balance_tracker._reserved_crypto = Decimal("1")
        sell_order = _make_order(OrderSide.SELL, price=200, status=OrderStatus.CLOSED)

        await balance_tracker.record_execution(sell_order, sell_order.filled)

        assert balance_tracker.total_fees == pytest.approx(0.3)
        assert balance_tracker.reserved_crypto == 0
        assert balance_tracker.balance == pytest.approx(1000 + 200 - 0.2)

    @pytest.mark.asyncio
    async def test_update_balance_on_order_completion_uses_average_fill_price(
        self, setup_balance_tracker, valid_config
    ):
        """Slipped fills must hit the balances at the actual fill price (order.average),
        not the requested price — otherwise backtest_slippage has zero P&L effect."""
        balance_tracker, _ = setup_balance_tracker
        balance_tracker.fee_calculator = _fees(valid_config, rate=0.0)
        balance_tracker._balance = Decimal("1000")
        balance_tracker._crypto_balance = Decimal("5")

        buy_order = _make_order(OrderSide.BUY, price=100, average=101.0, status=OrderStatus.CLOSED)
        balance_tracker._reserved_fiat = Decimal("101")
        await balance_tracker.record_execution(buy_order, buy_order.filled)
        assert balance_tracker.reserved_fiat == 0  # 101 consumed at the slipped price, not 100
        assert balance_tracker.crypto_balance == 6

        sell_order = _make_order(OrderSide.SELL, price=200, average=198.0, status=OrderStatus.CLOSED)
        balance_tracker._reserved_crypto = Decimal("1")
        await balance_tracker.record_execution(sell_order, sell_order.filled)
        assert balance_tracker.balance == 1198  # proceeds at 198, not 200
        assert balance_tracker.reserved_crypto == 0

    def test_get_total_balance_value(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("1000")
        balance_tracker._crypto_balance = Decimal("5")
        assert balance_tracker.get_total_balance_value(price=200) == 2000

    @pytest.mark.asyncio
    async def test_setup_balances_backtest(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker.trading_mode = TradingMode.BACKTEST

        await balance_tracker.setup_balances(initial_balance=2000, initial_crypto_balance=10)

        assert balance_tracker.balance == 2000
        assert balance_tracker.crypto_balance == 10

    @pytest.mark.asyncio
    async def test_setup_balances_live_mode(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
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
        balance_tracker, _ = setup_balance_tracker

        balance_tracker.trading_mode = TradingMode.PAPER_TRADING
        await balance_tracker.setup_balances(
            initial_balance=2000,
            initial_crypto_balance=10,
        )

        assert balance_tracker.balance == 2000
        assert balance_tracker.crypto_balance == 10

    @pytest.mark.asyncio
    async def test_fetch_live_balances_success(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
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
        balance_tracker, _ = setup_balance_tracker
        mock_exchange_service = AsyncMock()
        mock_exchange_service.get_balance.return_value = None

        with pytest.raises(ValueError, match="Unexpected balance structure: None"):
            await balance_tracker._fetch_live_balances(exchange_service=mock_exchange_service)

    # ── release_reserved_fiat ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_reserved_fiat_full(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("800")
        balance_tracker._reserved_fiat = Decimal("200")

        await balance_tracker.release_reserved_fiat(200)

        assert balance_tracker.balance == 1000
        assert balance_tracker.reserved_fiat == 0

    @pytest.mark.asyncio
    async def test_release_reserved_fiat_partial(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("800")
        balance_tracker._reserved_fiat = Decimal("200")

        await balance_tracker.release_reserved_fiat(100)

        assert balance_tracker.balance == 900
        assert balance_tracker.reserved_fiat == 100

    @pytest.mark.asyncio
    async def test_release_reserved_fiat_over_release(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._balance = Decimal("800")
        balance_tracker._reserved_fiat = Decimal("200")

        await balance_tracker.release_reserved_fiat(500)

        assert balance_tracker.balance == 1000
        assert balance_tracker.reserved_fiat == 0

    # ── release_reserved_crypto ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_reserved_crypto_full(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("3")
        balance_tracker._reserved_crypto = Decimal("2")

        await balance_tracker.release_reserved_crypto(2)

        assert balance_tracker.crypto_balance == 5
        assert balance_tracker.reserved_crypto == 0

    @pytest.mark.asyncio
    async def test_release_reserved_crypto_partial(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("3")
        balance_tracker._reserved_crypto = Decimal("2")

        await balance_tracker.release_reserved_crypto(1)

        assert balance_tracker.crypto_balance == 4
        assert balance_tracker.reserved_crypto == 1

    @pytest.mark.asyncio
    async def test_release_reserved_crypto_over_release(self, setup_balance_tracker):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._crypto_balance = Decimal("3")
        balance_tracker._reserved_crypto = Decimal("2")

        await balance_tracker.release_reserved_crypto(10)

        assert balance_tracker.crypto_balance == 5
        assert balance_tracker.reserved_crypto == 0

    async def test_a_release_above_the_reserve_only_below_the_booked_precision_empties_it_quietly(
        self, setup_balance_tracker, caplog
    ):
        balance_tracker, _ = setup_balance_tracker
        balance_tracker._reserved_fiat = Decimal("17.82336485")
        balance_tracker._reserved_crypto = Decimal("0.17391304")

        await balance_tracker.release_reserved_fiat(17.823364850075716)
        await balance_tracker.release_reserved_crypto(0.173913043478)

        assert (balance_tracker.reserved_fiat, balance_tracker.balance) == (0.0, 17.82336485)
        assert (balance_tracker.reserved_crypto, balance_tracker.crypto_balance) == (0.0, 0.17391304)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.fixture
def balance_tracker(valid_config):
    fee_calculator = _fees(valid_config)
    bt = BalanceTracker(
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


class TestRecordExecution:
    async def test_only_the_given_quantity_is_booked(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(OrderSide.BUY, average=100.0, filled=1.0, remaining=0.0, status=OrderStatus.CLOSED)

        await balance_tracker.record_execution(order, 0.6)

        assert balance_tracker.crypto_balance == pytest.approx(0.6 * 0.999)

    async def test_an_open_order_keeps_the_reservation_of_its_remainder(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(OrderSide.BUY, average=100.0, filled=0.4, remaining=0.6, status=OrderStatus.OPEN)

        await balance_tracker.record_execution(order, 0.4)

        assert balance_tracker.reserved_fiat == pytest.approx(100.0 - 40.0)


class TestRecordEndedOrder:
    async def test_zero_fill_buy_releases_full_reservation(self, balance_tracker):
        initial_balance = balance_tracker.balance
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(
            side=OrderSide.BUY, price=100.0, amount=1.0, filled=0.0, remaining=1.0, status=OrderStatus.CANCELED
        )

        await balance_tracker.record_execution(order, order.filled)

        assert balance_tracker.reserved_fiat == 0.0
        assert balance_tracker.balance == pytest.approx(initial_balance)

    async def test_partial_fill_buy_credits_crypto_and_releases_remainder(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(
            side=OrderSide.BUY,
            price=100.0,
            average=100.0,
            amount=1.0,
            filled=0.4,
            remaining=0.6,
            status=OrderStatus.CANCELED,
        )

        await balance_tracker.record_execution(order, order.filled)

        assert balance_tracker.crypto_balance == pytest.approx(0.4 * 0.999)
        # reserved consumed for the filled 40 and released for the remaining 60
        assert balance_tracker.reserved_fiat == pytest.approx(0.0, abs=1e-6)

    async def test_partial_fill_sell_credits_fiat_and_releases_remainder(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto  # 2.0 crypto, 0 fiat
        await bt.reserve_funds_for_sell(1.0)
        order = _make_order(
            side=OrderSide.SELL,
            price=100.0,
            average=100.0,
            amount=1.0,
            filled=0.3,
            remaining=0.7,
            status=OrderStatus.CANCELED,
        )

        await bt.record_execution(order, order.filled)

        assert bt.reserved_crypto == pytest.approx(0.0, abs=1e-8)
        assert bt.crypto_balance == pytest.approx(1.7)
        assert bt.balance > 0  # proceeds of 0.3 * 100 minus fee

    async def test_closed_order_with_a_remainder_releases_it(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        order = _make_order(
            side=OrderSide.BUY,
            price=100.0,
            average=100.0,
            amount=1.0,
            filled=0.4,
            remaining=0.6,
            status=OrderStatus.CLOSED,
        )

        await balance_tracker.record_execution(order, order.filled)

        assert balance_tracker.crypto_balance == pytest.approx(0.4 * 0.999)
        assert balance_tracker.reserved_fiat == pytest.approx(0.0, abs=1e-6)
        assert balance_tracker.balance == pytest.approx(1000.0 - 40.0)


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

        fee = bt.fee_calculator.calculate_fee(2.0 * 99.0, Liquidity.TAKER)
        assert bt.balance == pytest.approx(2.0 * 99.0 - fee)
        assert bt.crypto_balance == 0.0

    async def test_a_leg_cancelled_after_a_partial_fill_books_what_it_sold(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        order = _make_order(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=OrderStatus.CANCELED,
            price=100.0,
            average=99.0,
            amount=2.0,
            filled=0.5,
            remaining=1.5,
        )

        await bt.update_after_liquidation(order)

        fee = bt.fee_calculator.calculate_fee(0.5 * 99.0, Liquidity.TAKER)
        assert bt.crypto_balance == pytest.approx(1.5)
        assert bt.balance == pytest.approx(0.5 * 99.0 - fee)

    async def test_a_liquidation_that_sold_nothing_is_refused(self, balance_tracker_with_crypto):
        order = _make_order(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=OrderStatus.CANCELED,
            price=100.0,
            average=None,
            amount=2.0,
            filled=0.0,
            remaining=2.0,
        )

        with pytest.raises(ValueError, match="sold nothing"):
            await balance_tracker_with_crypto.update_after_liquidation(order)


class TestBalanceTrackerLiquidityAwareFees:
    """
    A fill must be charged at the rate matching its liquidity role: resting limit orders
    pay the maker fee, market orders that cross the book pay the taker fee.
    """

    MAKER_RATE = 0.001
    TAKER_RATE = 0.005

    @pytest.fixture
    def tracker(self, valid_config):
        config = config_manager_from(
            valid_config,
            {
                "exchange": {
                    "trading_fee": self.MAKER_RATE,
                    "maker_fee": self.MAKER_RATE,
                    "taker_fee": self.TAKER_RATE,
                },
                "pair": {"base_currency": "BTC", "quote_currency": "USDT"},
            },
        )
        bt = BalanceTracker(
            fee_calculator=FeeCalculator(config),
            trading_mode=TradingMode.LIVE,
            base_currency="BTC",
            quote_currency="USDT",
        )
        bt._balance = Decimal("100000")
        return bt

    @pytest.mark.asyncio
    async def test_filled_limit_buy_pays_the_maker_fee(self, tracker):
        tracker._reserved_fiat = Decimal("1000")
        order = _make_order(
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            status=OrderStatus.CLOSED,
            price=100.0,
            average=100.0,
        )

        await tracker.record_execution(order, order.filled)

        assert float(tracker.total_fees) == pytest.approx(100.0 * self.MAKER_RATE)

    @pytest.mark.asyncio
    async def test_filled_market_buy_pays_the_taker_fee(self, tracker):
        tracker._reserved_fiat = Decimal("1000")
        order = _make_order(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            price=100.0,
            average=100.0,
        )

        await tracker.record_execution(order, order.filled)

        assert float(tracker.total_fees) == pytest.approx(100.0 * self.TAKER_RATE)

    @pytest.mark.asyncio
    async def test_filled_limit_sell_pays_the_maker_fee(self, tracker):
        tracker._reserved_crypto = Decimal("1")
        order = _make_order(
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            status=OrderStatus.CLOSED,
            price=200.0,
            average=200.0,
        )

        await tracker.record_execution(order, order.filled)

        assert float(tracker.total_fees) == pytest.approx(200.0 * self.MAKER_RATE)

    @pytest.mark.asyncio
    async def test_initial_market_purchase_pays_the_taker_fee(self, tracker):
        order = _make_order(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            price=100.0,
            average=100.0,
            amount=2.0,
            filled=2.0,
        )

        await tracker.update_after_initial_purchase(order)

        assert float(tracker.total_fees) == pytest.approx(2.0 * 100.0 * self.TAKER_RATE)

    @pytest.mark.asyncio
    async def test_liquidation_pays_the_taker_fee(self, tracker):
        tracker._crypto_balance = Decimal("2")
        order = _make_order(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            price=100.0,
            average=99.0,
            amount=2.0,
            filled=2.0,
        )

        await tracker.update_after_liquidation(order)

        assert float(tracker.total_fees) == pytest.approx(2.0 * 99.0 * self.TAKER_RATE)


class TestAccountSnapshot:
    def test_peak_rises_and_drawdown_measures_the_fall_from_it(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto  # 2 crypto, 0 fiat

        first = bt.snapshot(100.0)
        bt.snapshot(150.0)
        low = bt.snapshot(120.0)

        assert (first.value, first.peak, first.drawdown_pct) == (200.0, 200.0, 0.0)
        assert low.value == pytest.approx(240.0)
        assert low.peak == pytest.approx(300.0)
        assert low.drawdown_pct == pytest.approx(20.0)

    def test_crypto_of_unknown_cost_starts_with_zero_unrealized_pnl(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto

        assert bt.snapshot(100.0).unrealized_pnl == 0.0
        assert bt.snapshot(110.0).unrealized_pnl == pytest.approx(20.0)

    def test_empty_account_has_zero_drawdown(self, balance_tracker):
        balance_tracker._balance = Decimal("0")

        snapshot = balance_tracker.snapshot(100.0)

        assert (snapshot.value, snapshot.peak, snapshot.drawdown_pct) == (0.0, 0.0, 0.0)

    async def test_setup_balances_resets_cost_realized_pnl_and_peak(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        bt.snapshot(100.0)
        bt._ledger.realized_pnl = Decimal("5")
        bt.trading_mode = TradingMode.PAPER_TRADING

        await bt.setup_balances(initial_balance=1000.0, initial_crypto_balance=0.0)

        snapshot = bt.snapshot(100.0)
        assert (snapshot.realized_pnl, snapshot.unrealized_pnl, snapshot.peak) == (0.0, 0.0, 1000.0)


class TestAccountingIdentity:
    async def test_value_change_equals_realized_plus_unrealized_pnl(self, balance_tracker):
        bt = balance_tracker  # 1000 fiat, 0 crypto, fee 0.1 % of the trade value
        rng = random.Random(7)  # noqa: S311
        price = 100.0
        value0 = bt.snapshot(price).value

        initial = _make_order(
            OrderSide.BUY, order_type=OrderType.MARKET, status=OrderStatus.CLOSED, price=price, average=price
        )
        await bt.update_after_initial_purchase(initial)

        for _ in range(300):
            price = max(1.0, price * (1 + rng.uniform(-0.03, 0.03)))
            action = rng.choice(["buy", "sell", "partial_buy", "partial_sell"])
            quantity = round(rng.uniform(0.01, 0.5), 6)
            if action in ("buy", "partial_buy") and bt.balance > quantity * price * 1.01:
                await bt.reserve_funds_for_buy(quantity * price)
                filled = quantity if action == "buy" else round(quantity * 0.4, 6)
                order = _make_order(
                    OrderSide.BUY,
                    price=price,
                    average=price,
                    amount=quantity,
                    filled=filled,
                    remaining=quantity - filled,
                )
                if action == "buy":
                    await bt.record_execution(order, order.filled)
                else:
                    order.status = OrderStatus.CANCELED
                    await bt.record_execution(order, order.filled)
            elif action in ("sell", "partial_sell") and bt.crypto_balance >= quantity:
                await bt.reserve_funds_for_sell(quantity)
                filled = quantity if action == "sell" else round(quantity * 0.4, 6)
                order = _make_order(
                    OrderSide.SELL,
                    price=price,
                    average=price,
                    amount=quantity,
                    filled=filled,
                    remaining=quantity - filled,
                )
                if action == "sell":
                    await bt.record_execution(order, order.filled)
                else:
                    order.status = OrderStatus.CANCELED
                    await bt.record_execution(order, order.filled)
            snapshot = bt.snapshot(price)
            # Each balance move is quantized to 1e-8 while the ledger is not, so the gap can grow by that per fill.
            assert snapshot.value - value0 == pytest.approx(snapshot.realized_pnl + snapshot.unrealized_pnl, abs=1e-5)

        position = bt.crypto_balance
        liquidation = _make_order(
            OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            price=price,
            average=price,
            amount=position,
            filled=position,
        )
        await bt.update_after_liquidation(liquidation)
        final = bt.snapshot(price)
        assert final.unrealized_pnl == 0.0
        assert final.value - value0 == pytest.approx(final.realized_pnl, abs=1e-5)


class TestRecoveryAccounting:
    def test_restore_accounting_sets_cost_realized_pnl_and_peak(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto  # 2 crypto, 0 fiat

        bt.restore_accounting(avg_cost=Decimal("90"), realized_pnl=Decimal("5"), peak_value=Decimal("250"))
        snapshot = bt.snapshot(100.0)

        assert snapshot.realized_pnl == 5.0
        assert snapshot.unrealized_pnl == pytest.approx(20.0)
        assert snapshot.peak == 250.0
        assert snapshot.drawdown_pct == pytest.approx(20.0)

    def test_replay_books_fills_in_execution_order_without_touching_balances(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        bt.restore_accounting(avg_cost=Decimal("100"), realized_pnl=Decimal("0"), peak_value=None)
        sell = _make_order(OrderSide.SELL, price=110.0, average=None, amount=1.0, filled=1.0, remaining=0.0)
        sell.last_trade_timestamp = 2
        buy = _make_order(OrderSide.BUY, price=90.0, average=90.0, amount=1.0, filled=1.0, remaining=0.0)
        buy.last_trade_timestamp = 1

        bt.replay_fills_on_cost_basis(Decimal("2"), [(sell, sell.filled), (buy, buy.filled)])

        # buy first: 0.999 more at a cost of 90, (2 x 100 + 90) / 2.999; then sell 1 at 110 with a 0.11 fee
        assert float(bt._ledger.avg_cost) == pytest.approx(96.6988996, abs=1e-6)
        assert float(bt._ledger.realized_pnl) == pytest.approx(13.1911004, abs=1e-6)
        assert bt._crypto_balance == Decimal("2.0")
        assert bt._balance == Decimal("0")
        assert bt.total_fees == pytest.approx(1.0 * 110.0 * 0.001 + 1.0 * 90.0 * 0.001)

    def test_replay_falls_back_to_limit_price_and_creation_time(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        bt.restore_accounting(avg_cost=Decimal("100"), realized_pnl=Decimal("0"), peak_value=None)
        sell = _make_order(OrderSide.SELL, price=110.0, average=None, amount=1.0, filled=1.0, remaining=0.0)
        sell.timestamp = 2
        buy = _make_order(OrderSide.BUY, price=90.0, average=None, amount=1.0, filled=1.0, remaining=0.0)
        buy.timestamp = 1

        bt.replay_fills_on_cost_basis(Decimal("2"), [(sell, sell.filled), (buy, buy.filled)])

        assert float(bt._ledger.realized_pnl) == pytest.approx(13.1911004, abs=1e-6)

    def test_replay_skips_orders_without_a_fill(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        bt.restore_accounting(avg_cost=Decimal("100"), realized_pnl=Decimal("0"), peak_value=None)
        unfilled = _make_order(OrderSide.SELL, price=110.0, amount=1.0, filled=0.0, remaining=1.0)

        bt.replay_fills_on_cost_basis(Decimal("2"), [(unfilled, unfilled.filled)])

        assert bt._ledger.realized_pnl == Decimal("0")
        assert bt._ledger.avg_cost == Decimal("100")

    def test_replay_books_fills_of_orders_without_any_timestamp(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        bt.restore_accounting(avg_cost=Decimal("100"), realized_pnl=Decimal("0"), peak_value=None)
        ghost = _make_order(OrderSide.BUY, price=90.0, amount=1.0, filled=0.0, remaining=1.0)
        sell = _make_order(OrderSide.SELL, price=110.0, average=110.0, amount=1.0, filled=1.0, remaining=0.0)
        for order in (ghost, sell):
            order.timestamp = None
            order.last_trade_timestamp = None

        bt.replay_fills_on_cost_basis(Decimal("2"), [(ghost, 0.0), (sell, sell.filled)])

        assert float(bt._ledger.realized_pnl) == pytest.approx(10.0 - 0.11)
        assert bt.total_fees == pytest.approx(0.11)


class TestFillAccountingFixes:
    async def test_a_sell_beyond_its_reservation_takes_the_excess_from_the_free_crypto(self, balance_tracker):
        bt = balance_tracker
        bt._crypto_balance = Decimal("2")
        await bt.reserve_funds_for_sell(1.0)
        value_before = bt.get_total_balance_value(100.0)
        sell = _make_order(
            OrderSide.SELL, price=100.0, average=100.0, amount=1.5, filled=1.5, status=OrderStatus.CLOSED
        )

        await bt.record_execution(sell, sell.filled)

        assert bt.reserved_crypto == 0.0
        assert bt.get_adjusted_crypto_balance() == pytest.approx(0.5)
        assert bt.get_total_balance_value(100.0) == pytest.approx(value_before - 0.15)

    async def test_a_partly_filled_initial_purchase_pays_the_fee_on_its_filled_quantity(self, balance_tracker):
        bt = balance_tracker
        purchase = _make_order(
            OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            price=100.0,
            average=100.0,
            amount=2.0,
            filled=1.0,
            remaining=1.0,
        )

        await bt.update_after_initial_purchase(purchase)

        assert bt.total_fees == pytest.approx(0.1)
        assert bt.balance == pytest.approx(1000.0 - 100.0)
        assert bt.crypto_balance == pytest.approx(1.0 * 0.999)

    async def test_an_initial_purchase_without_an_average_price_is_refused(self, balance_tracker):
        bt = balance_tracker
        purchase = _make_order(
            OrderSide.BUY, order_type=OrderType.MARKET, status=OrderStatus.CLOSED, average=None, filled=1.0
        )

        with pytest.raises(ValueError, match="no filled quantity or average price"):
            await bt.update_after_initial_purchase(purchase)

        assert bt.balance == 1000.0
        assert bt.crypto_balance == 0.0
        assert bt.total_fees == 0.0


class TestFeesInTheCurrencyReceived:
    async def test_a_buy_takes_its_cost_from_the_reservation_and_its_fee_from_the_crypto(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        buy = _make_order(OrderSide.BUY, average=100.0, status=OrderStatus.CLOSED)

        await balance_tracker.record_execution(buy, buy.filled)

        assert balance_tracker.reserved_fiat == 0.0
        assert balance_tracker.balance == pytest.approx(900.0)
        assert balance_tracker.crypto_balance == pytest.approx(0.999)
        assert balance_tracker.total_fees == pytest.approx(0.1)

    async def test_the_fee_the_exchange_reports_replaces_the_model(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        buy = _make_order(
            OrderSide.BUY, average=100.0, status=OrderStatus.CLOSED, fee={"cost": 0.002, "currency": "BTC"}
        )

        await balance_tracker.record_execution(buy, buy.filled)

        assert balance_tracker.crypto_balance == pytest.approx(0.998)
        assert balance_tracker.total_fees == pytest.approx(0.2)

    async def test_a_buy_fee_reported_in_quote_comes_out_of_the_free_fiat(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        buy = _make_order(
            OrderSide.BUY, average=100.0, status=OrderStatus.CLOSED, fee={"cost": 0.3, "currency": "USDT"}
        )

        await balance_tracker.record_execution(buy, buy.filled)

        assert balance_tracker.reserved_fiat == 0.0
        assert balance_tracker.balance == pytest.approx(900.0 - 0.3)
        assert balance_tracker.crypto_balance == pytest.approx(1.0)
        assert balance_tracker.total_fees == pytest.approx(0.3)

    async def test_a_fee_paid_in_another_currency_touches_neither_balance(self, balance_tracker, caplog):
        caplog.set_level(logging.INFO)
        await balance_tracker.reserve_funds_for_buy(100.0)
        buy = _make_order(
            OrderSide.BUY, average=100.0, status=OrderStatus.CLOSED, fee={"cost": 0.0001, "currency": "BNB"}
        )

        await balance_tracker.record_execution(buy, buy.filled)

        assert (balance_tracker.balance, balance_tracker.crypto_balance) == (pytest.approx(900.0), 1.0)
        assert balance_tracker.total_fees == 0.0
        assert "BNB" in caplog.text

    async def test_the_reported_total_is_charged_once_over_earlier_partial_fills(self, balance_tracker):
        await balance_tracker.reserve_funds_for_buy(100.0)
        partial = _make_order(OrderSide.BUY, average=100.0, filled=0.4, remaining=0.6, status=OrderStatus.OPEN)
        await balance_tracker.record_execution(partial, 0.4)
        partial.filled, partial.remaining, partial.status = 1.0, 0.0, OrderStatus.CLOSED
        partial.fee = {"cost": 0.002, "currency": "BTC"}

        await balance_tracker.record_execution(partial, 0.6)

        assert balance_tracker.crypto_balance == pytest.approx(1.0 - 0.002)
        assert balance_tracker.reserved_fiat == 0.0

    async def test_a_sell_overflow_never_leaves_negative_free_crypto(self, balance_tracker, caplog):
        balance_tracker._reserved_crypto = Decimal("1")
        sell = _make_order(OrderSide.SELL, average=100.0, amount=1.5, filled=1.5, status=OrderStatus.CLOSED)

        await balance_tracker.record_execution(sell, sell.filled)

        assert balance_tracker.crypto_balance == 0.0
        assert balance_tracker.reserved_crypto == 0.0
        assert any(r.levelname == "WARNING" and "free" in r.getMessage() for r in caplog.records)

    async def test_the_initial_purchase_pays_its_fee_in_base(self, balance_tracker):
        purchase = _make_order(
            OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.CLOSED,
            average=100.0,
            amount=2.0,
            filled=2.0,
            fee={"cost": 0.003, "currency": "BTC"},
        )

        await balance_tracker.update_after_initial_purchase(purchase)

        assert balance_tracker.balance == pytest.approx(800.0)
        assert balance_tracker.crypto_balance == pytest.approx(1.997)
        assert balance_tracker.total_fees == pytest.approx(0.3)

    def test_a_replayed_buy_moves_the_position_by_what_it_received(self, balance_tracker_with_crypto):
        bt = balance_tracker_with_crypto
        bt.restore_accounting(avg_cost=Decimal("100"), realized_pnl=Decimal("0"), peak_value=None)
        buy = _make_order(OrderSide.BUY, price=90.0, average=90.0, status=OrderStatus.CLOSED)
        buy.last_trade_timestamp = 1
        sell = _make_order(OrderSide.SELL, price=110.0, average=110.0, amount=2.999, filled=2.999)
        sell.status = OrderStatus.CLOSED
        sell.last_trade_timestamp = 2

        bt.replay_fills_on_cost_basis(Decimal("2"), [(buy, 1.0), (sell, 2.999)])

        # The whole position (2 + 0.999) is sold, so the cost basis resets.
        assert bt._ledger.avg_cost == Decimal("0")
        assert bt._crypto_balance == Decimal("2.0")
