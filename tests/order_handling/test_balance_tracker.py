from unittest.mock import AsyncMock, Mock

import pytest

from grid_trading_bot.config.trading_mode import TradingMode
from grid_trading_bot.core.bot_management.event_bus import EventBus, Events
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.fee_calculator import FeeCalculator
from grid_trading_bot.core.order_handling.order import OrderSide
from grid_trading_bot.core.validation.exceptions import (
    InsufficientBalanceError,
    InsufficientCryptoBalanceError,
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
        balance_tracker._balance = 1000

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
        balance_tracker._crypto_balance = 5

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
        balance_tracker._balance = 1000

        await balance_tracker.reserve_funds_for_buy(200)

        assert balance_tracker.get_adjusted_fiat_balance() == 1000

    @pytest.mark.asyncio
    async def test_get_adjusted_crypto_balance(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._crypto_balance = 5

        await balance_tracker.reserve_funds_for_sell(2)

        assert balance_tracker.get_adjusted_crypto_balance() == 5

    def test_update_after_buy_order_filled(self, setup_balance_tracker):
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._crypto_balance = 5
        fee_calculator.calculate_fee.return_value = 10
        balance_tracker._reserved_fiat = 500

        balance_tracker._update_after_buy_order_filled(quantity=1, price=100)

        assert balance_tracker.crypto_balance == 6
        assert balance_tracker.total_fees == 10
        assert balance_tracker.reserved_fiat == 390

    def test_update_after_sell_order_filled(self, setup_balance_tracker):
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._balance = 1000
        fee_calculator.calculate_fee.return_value = 10
        balance_tracker._reserved_crypto = 2

        balance_tracker._update_after_sell_order_filled(quantity=1, price=200)

        assert balance_tracker.balance == 1190
        assert balance_tracker.total_fees == 10
        assert balance_tracker.reserved_crypto == 1

    @pytest.mark.asyncio
    async def test_update_balance_on_order_completion(self, setup_balance_tracker):
        balance_tracker, fee_calculator, _ = setup_balance_tracker
        balance_tracker._balance = 1000
        balance_tracker._crypto_balance = 5
        fee_calculator.calculate_fee.return_value = 5  # Mock fee calculation

        buy_order = Mock(side=OrderSide.BUY, filled=1, price=100)
        balance_tracker._reserved_fiat = 105  # Reserved fiat for the buy order (price + fee)
        await balance_tracker._update_balance_on_order_completion(buy_order)
        assert balance_tracker.crypto_balance == 6  # Crypto balance increases by 1
        assert balance_tracker.total_fees == 5  # Total fees reflect the buy order fee
        assert balance_tracker.reserved_fiat == 0  # Reserved fiat should be fully consumed

        sell_order = Mock(side=OrderSide.SELL, filled=1, price=200)
        balance_tracker._reserved_crypto = 1  # Reserved crypto for the sell order
        await balance_tracker._update_balance_on_order_completion(sell_order)
        assert balance_tracker.total_fees == 10  # Total fees include the sell order fee
        assert balance_tracker.reserved_crypto == 0  # Reserved crypto should be fully consumed
        assert balance_tracker.balance == 1195  # Remaining balance after the sell order

    def test_get_total_balance_value(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        balance_tracker._balance = 1000
        balance_tracker._crypto_balance = 5
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
        balance_tracker._fetch_live_balances = AsyncMock(return_value=(1500, 5))

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
        assert balances == (1000, 5)

    @pytest.mark.asyncio
    async def test_fetch_live_balances_unexpected_structure(self, setup_balance_tracker):
        balance_tracker, _, _ = setup_balance_tracker
        mock_exchange_service = AsyncMock()
        mock_exchange_service.get_balance.return_value = None

        with pytest.raises(ValueError, match="Unexpected balance structure: None"):
            await balance_tracker._fetch_live_balances(exchange_service=mock_exchange_service)
