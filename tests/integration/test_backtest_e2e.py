"""End-to-end integration tests for backtest mode.

These tests run the full bot stack (config → exchange service → grid manager →
order manager → balance tracker → performance analyzer) with **zero mocks**.
Only the plotting is disabled.
"""

import logging

import pytest

from grid_trading_bot.core.grid_management.grid_level import GridCycleState
from grid_trading_bot.core.order_handling.order import OrderSide

from .snapshot_utils import (
    compare_snapshots,
    extract_snapshot_data,
    load_snapshot,
    save_snapshot,
)

SCENARIOS = [
    ("simple_grid", "arithmetic"),
    ("simple_grid", "geometric"),
    ("hedged_grid", "arithmetic"),
    ("hedged_grid", "geometric"),
]


def _snapshot_name(strategy_type: str, spacing: str) -> str:
    return f"{strategy_type}_{spacing}"


# ---------------------------------------------------------------------------
# F1-F4: Bot completes without error
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_completes_without_error(strategy_type, spacing, run_backtest_bot):
    """The bot starts, runs a full backtest, and returns a valid result."""
    _bot, result = await run_backtest_bot(strategy_type, spacing)

    assert result is not None, "run() returned None — backtest did not produce results"
    assert "performance_summary" in result
    assert "orders" in result

    summary = result["performance_summary"]
    expected_keys = {"ROI", "Total Fees", "Final Balance (Fiat)", "Number of Buy Trades", "Number of Sell Trades"}
    missing = expected_keys - set(summary.keys())
    assert not missing, f"Performance summary missing keys: {missing}"

    assert len(result["orders"]) > 0, "No trades were executed during backtest"


@pytest.mark.integration
async def test_backtest_trades_when_price_stays_above_grid_center(run_backtest_bot):
    """Regression: a market holding above the grid's central price but inside the
    range must still arm the grid and trade. The old upward-trigger-crossing rule
    idled forever in this situation (observed as 500/500 zero-trade walk-forward
    test windows in grid_sweep)."""
    _bot, result = await run_backtest_bot("simple_grid", "arithmetic", range={"top": 175, "bottom": 120})

    assert len(result["orders"]) > 0, (
        "Grid never armed: no trades although the price stayed inside the grid range the whole backtest"
    )


@pytest.mark.integration
async def test_backtest_slippage_degrades_results(run_backtest_bot):
    """Regression: backtest_slippage must actually hit the P&L. It used to be applied
    to order.average only, while balances and gains read order.price — so any slippage
    value produced results identical to zero slippage."""
    _, result_no_slip = await run_backtest_bot("simple_grid", "arithmetic")
    _, result_slip = await run_backtest_bot("simple_grid", "arithmetic", execution={"backtest_slippage": 0.01})

    roi_no_slip = float(result_no_slip["performance_summary"]["ROI"].strip("%"))
    roi_slip = float(result_slip["performance_summary"]["ROI"].strip("%"))

    # 1% slippage on every fill must cost well more than the initial-purchase-only
    # effect (~0.5%) that existed while grid fills ignored the slipped price.
    assert roi_slip < roi_no_slip - 1.0, (
        f"Slippage had no meaningful P&L effect: ROI {roi_no_slip}% without vs {roi_slip}% with 1% slippage"
    )


# ---------------------------------------------------------------------------
# V2: Balance coherence after backtest
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_balance_coherence(strategy_type, spacing, run_backtest_bot):
    """After a backtest, balances are non-negative and reservations accounted for."""
    bot, result = await run_backtest_bot(strategy_type, spacing)
    bt = bot.balance_tracker
    order_book = bot.strategy.order_manager.order_book

    # All individual balances (including reserved) must be non-negative
    assert bt.balance >= 0, f"Fiat balance is negative: {bt.balance}"
    assert bt.crypto_balance >= 0, f"Crypto balance is negative: {bt.crypto_balance}"
    assert bt.reserved_fiat >= 0, f"Reserved fiat is negative: {bt.reserved_fiat}"
    assert bt.reserved_crypto >= 0, f"Reserved crypto is negative: {bt.reserved_crypto}"

    # Reserved amounts must be backed by open orders
    open_buy_orders = [o for o in order_book.get_all_buy_orders() if o.is_open()]
    open_sell_orders = [o for o in order_book.get_all_sell_orders() if o.is_open()]

    if not open_buy_orders:
        assert bt.reserved_fiat == 0, f"Reserved fiat={bt.reserved_fiat} but no open buy orders"
    if not open_sell_orders:
        assert bt.reserved_crypto == 0, f"Reserved crypto={bt.reserved_crypto} but no open sell orders"

    # Total adjusted balances must be non-negative
    assert bt.get_adjusted_fiat_balance() >= 0, "Adjusted fiat balance (fiat + reserved) is negative"
    assert bt.get_adjusted_crypto_balance() >= 0, "Adjusted crypto balance (crypto + reserved) is negative"

    # Cross-check: total value from balance_tracker vs performance summary
    summary = result["performance_summary"]
    reported_final = float(summary["Final Balance (Fiat)"].split()[0])
    final_price = bot.strategy.close_prices[-1]
    computed_total = bt.get_total_balance_value(final_price)
    assert abs(computed_total - reported_final) < 0.02, (
        f"Balance mismatch: tracker says {computed_total:.4f}, summary says {reported_final:.4f}"
    )

    assert bot.strategy.data["account_value"].notna().all(), "Some candles have no account value"


def _assert_value_change_splits_into_realized_and_unrealized(bot, result):
    final_price = bot.strategy.close_prices[-1]
    snapshot = bot.balance_tracker.snapshot(final_price)

    value_change = snapshot.value - bot.config_manager.get_initial_balance()
    # BalanceTracker rounds the crypto position to 1e-8 per fill while the ledger keeps the exact quantity.
    assert value_change == pytest.approx(snapshot.realized_pnl + snapshot.unrealized_pnl, abs=1e-4)

    summary = result["performance_summary"]
    assert summary["Grid Trading Gains"] == f"{snapshot.realized_pnl:.2f}"
    assert summary["Unrealized PnL"] == f"{snapshot.unrealized_pnl:.2f}"
    return snapshot


def _error_records(caplog):
    return [f"{record.name}: {record.getMessage()}" for record in caplog.records if record.levelno >= logging.ERROR]


def _assert_reserved_matches_open_orders(bot):
    balances = bot.balance_tracker
    order_manager = bot.strategy.order_manager
    open_orders = order_manager.order_book.get_open_orders()
    open_buys = sum(o.price * o.remaining for o in open_orders if o.side == OrderSide.BUY)
    open_sells = sum(o.remaining for o in open_orders if o.side == OrderSide.SELL)
    assert len(order_manager.journal) == 0
    assert balances.reserved_fiat == pytest.approx(open_buys, abs=1e-6)
    assert balances.reserved_crypto == pytest.approx(open_sells, abs=1e-6)


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_logs_no_error_and_reserves_exactly_its_open_orders(
    strategy_type, spacing, run_backtest_bot, caplog
):
    """A refused order is an expected outcome, never an ERROR, and the reservations equal what the open orders need."""
    caplog.set_level(logging.WARNING)
    bot, _ = await run_backtest_bot(strategy_type, spacing)

    assert _error_records(caplog) == []
    _assert_reserved_matches_open_orders(bot)


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_value_change_splits_into_realized_and_unrealized(strategy_type, spacing, run_backtest_bot):
    """Realized plus unrealized PnL accounts for the whole change in account value."""
    bot, result = await run_backtest_bot(strategy_type, spacing)
    _assert_value_change_splits_into_realized_and_unrealized(bot, result)


# ---------------------------------------------------------------------------
# V4: Grid cycle integrity
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_grid_cycle_integrity(strategy_type, spacing, run_backtest_bot, caplog):
    """Every grid level agrees with its open orders and no strategy rule had to drop a paired order."""
    caplog.set_level(logging.INFO)
    bot, _ = await run_backtest_bot(strategy_type, spacing)
    grid_manager = bot.strategy.grid_manager
    order_book = bot.strategy.order_manager.order_book

    open_orders_by_price: dict[float, list] = {}
    for order in order_book.get_open_orders():
        level = order_book.get_grid_level_for_order(order)
        if level is not None:
            open_orders_by_price.setdefault(level.price, []).append(order)

    for price, level in grid_manager.grid_levels.items():
        orders = open_orders_by_price.get(price, [])
        if not orders:
            assert level.state == level.home_state, f"Level {price} holds no order but is {level.state.name}"
            continue
        sides = {order.side for order in orders}
        assert len(sides) == 1, f"Level {price} holds both buy and sell orders"
        expected = (
            GridCycleState.WAITING_FOR_BUY_FILL if OrderSide.BUY in sides else GridCycleState.WAITING_FOR_SELL_FILL
        )
        assert level.state == expected, f"Level {price} is {level.state.name} with open {sides} orders"

    if strategy_type == "hedged_grid":
        assert all(len(orders) == 1 for orders in open_orders_by_price.values()), "A hedged level holds two orders"
        assert len(open_orders_by_price) < len(grid_manager.grid_levels), "No free level left in the hedged grid"
    else:
        for price, orders in open_orders_by_price.items():
            expected_side = OrderSide.BUY if price <= grid_manager.central_price else OrderSide.SELL
            assert all(order.side == expected_side for order in orders), f"Level {price} trades on the wrong half"

    dropped = [r.getMessage() for r in caplog.records if r.name == "GridManager" and r.levelno >= logging.ERROR]
    assert not dropped, f"Strategy dropped paired orders: {dropped}"


# ---------------------------------------------------------------------------
# V3: Deterministic snapshot results
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_deterministic_results(strategy_type, spacing, run_backtest_bot, snapshot_dir, update_snapshots):
    """With identical config and CSV data, the backtest produces identical results."""
    _bot, result = await run_backtest_bot(strategy_type, spacing)
    name = _snapshot_name(strategy_type, spacing)
    actual = extract_snapshot_data(result["performance_summary"], result["orders"])

    if update_snapshots:
        save_snapshot(name, actual, snapshot_dir)
        pytest.skip(f"Snapshot updated for {name}")

    expected = load_snapshot(name, snapshot_dir)
    if expected is None:
        save_snapshot(name, actual, snapshot_dir)
        pytest.skip(f"Snapshot created for {name} (first run). Re-run to validate.")

    diffs = compare_snapshots(actual, expected)
    assert not diffs, f"Snapshot mismatch for {name}:\n" + "\n".join(diffs)


# ---------------------------------------------------------------------------
# Asymmetric ratio: sell_ratio=0.5 (crypto accumulation)
# ---------------------------------------------------------------------------

RATIO_SCENARIOS = [
    ("simple_grid", "arithmetic"),
    ("hedged_grid", "arithmetic"),
]


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), RATIO_SCENARIOS)
async def test_asymmetric_ratio_completes_without_error(strategy_type, spacing, run_backtest_bot):
    """The bot runs a full backtest with sell_ratio=0.5 without crashing."""
    _bot, result = await run_backtest_bot(strategy_type, spacing, sell_ratio=0.5)

    assert result is not None
    assert "performance_summary" in result
    assert len(result["orders"]) > 0


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), RATIO_SCENARIOS)
async def test_asymmetric_ratio_balance_coherence(strategy_type, spacing, run_backtest_bot):
    """With sell_ratio=0.5, all balances remain non-negative and consistent."""
    bot, result = await run_backtest_bot(strategy_type, spacing, sell_ratio=0.5)
    bt = bot.balance_tracker
    order_book = bot.strategy.order_manager.order_book

    assert bt.balance >= 0, f"Fiat balance is negative: {bt.balance}"
    assert bt.crypto_balance >= 0, f"Crypto balance is negative: {bt.crypto_balance}"
    assert bt.reserved_fiat >= 0, f"Reserved fiat is negative: {bt.reserved_fiat}"
    assert bt.reserved_crypto >= 0, f"Reserved crypto is negative: {bt.reserved_crypto}"

    open_buy_orders = [o for o in order_book.get_all_buy_orders() if o.is_open()]
    open_sell_orders = [o for o in order_book.get_all_sell_orders() if o.is_open()]

    if not open_buy_orders:
        assert bt.reserved_fiat == 0, f"Reserved fiat={bt.reserved_fiat} but no open buy orders"
    if not open_sell_orders:
        assert bt.reserved_crypto == 0, f"Reserved crypto={bt.reserved_crypto} but no open sell orders"

    assert bt.get_adjusted_fiat_balance() >= 0, "Adjusted fiat balance is negative"
    assert bt.get_adjusted_crypto_balance() >= 0, "Adjusted crypto balance is negative"

    summary = result["performance_summary"]
    reported_final = float(summary["Final Balance (Fiat)"].split()[0])
    final_price = bot.strategy.close_prices[-1]
    computed_total = bt.get_total_balance_value(final_price)
    assert abs(computed_total - reported_final) < 0.02, (
        f"Balance mismatch: tracker says {computed_total:.4f}, summary says {reported_final:.4f}"
    )


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), RATIO_SCENARIOS)
async def test_asymmetric_ratio_reduces_sell_volume(strategy_type, spacing, run_backtest_bot):
    """With sell_ratio=0.5, total sell volume is strictly lower than symmetric."""
    bot_asym, _ = await run_backtest_bot(strategy_type, spacing, sell_ratio=0.5)
    bot_sym, _ = await run_backtest_bot(strategy_type, spacing)

    ob_asym = bot_asym.strategy.order_manager.order_book
    ob_sym = bot_sym.strategy.order_manager.order_book

    asym_sell_vol = sum(o.filled for o in ob_asym.get_all_sell_orders() if o.is_filled())
    sym_sell_vol = sum(o.filled for o in ob_sym.get_all_sell_orders() if o.is_filled())
    assert asym_sell_vol < sym_sell_vol, (
        f"Asymmetric sell volume should be lower: asym={asym_sell_vol:.6f} vs sym={sym_sell_vol:.6f}"
    )

    # The initial purchase only funds the sell orders, which are half-size
    asym_initial = sum(o.filled for o, level in ob_asym.get_buy_orders_with_grid() if level is None)
    sym_initial = sum(o.filled for o, level in ob_sym.get_buy_orders_with_grid() if level is None)
    assert asym_initial == pytest.approx(sym_initial * 0.5), (
        f"Asymmetric initial purchase should be half the symmetric one: asym={asym_initial:.6f} vs "
        f"sym={sym_initial:.6f}"
    )


# ---------------------------------------------------------------------------
# ATR trailing stop-loss + dynamic spacing (feature-ON E2E)
# ---------------------------------------------------------------------------

ATR_TRAILING_STOP_LOSS = {
    "enabled": True,
    "atr_period": 14,
    "atr_multiplier": 2.5,
    "on_trigger": "regrid",
}

ATR_DYNAMIC_SPACING = {
    "enabled": True,
    "atr_period": 14,
    "atr_spacing_multiplier": 1.0,
    "regrid_threshold": 0.3,
    "cooldown_bars": 60,
}

ATR_SNAPSHOT_NAME = "simple_grid_arithmetic_atr"


def _run_atr_scenario(run_backtest_bot):
    return run_backtest_bot(
        "simple_grid",
        "arithmetic",
        risk_management_overrides={"trailing_stop_loss": ATR_TRAILING_STOP_LOSS},
        dynamic_spacing=ATR_DYNAMIC_SPACING,
    )


@pytest.mark.integration
async def test_atr_scenario_completes_without_error(run_backtest_bot):
    """The bot runs with trailing stop-loss (on_trigger=regrid) + dynamic spacing enabled without crashing."""
    _bot, result = await _run_atr_scenario(run_backtest_bot)

    assert result is not None, "run() returned None — backtest did not produce results"
    assert "performance_summary" in result
    assert "orders" in result
    assert len(result["orders"]) > 0, "No trades were executed during backtest"


@pytest.mark.integration
async def test_atr_scenario_logs_no_error_and_reports_unfunded_levels_once_per_round(run_backtest_bot, caplog):
    """Each regrid that cannot fund every level says so once, at WARNING, instead of one ERROR per level."""
    caplog.set_level(logging.WARNING)
    bot, _ = await _run_atr_scenario(run_backtest_bot)

    assert _error_records(caplog) == []
    summaries = [record for record in caplog.records if "levels unfunded" in record.getMessage()]
    assert summaries
    assert all(record.levelno == logging.WARNING for record in summaries)
    _assert_reserved_matches_open_orders(bot)


@pytest.mark.integration
async def test_atr_scenario_balance_coherence(run_backtest_bot):
    """After the ATR scenario backtest, balances are non-negative and reservations accounted for."""
    bot, result = await _run_atr_scenario(run_backtest_bot)
    bt = bot.balance_tracker
    order_book = bot.strategy.order_manager.order_book

    assert bt.balance >= 0, f"Fiat balance is negative: {bt.balance}"
    assert bt.crypto_balance >= 0, f"Crypto balance is negative: {bt.crypto_balance}"
    assert bt.reserved_fiat >= 0, f"Reserved fiat is negative: {bt.reserved_fiat}"
    assert bt.reserved_crypto >= 0, f"Reserved crypto is negative: {bt.reserved_crypto}"

    open_buy_orders = [o for o in order_book.get_all_buy_orders() if o.is_open()]
    open_sell_orders = [o for o in order_book.get_all_sell_orders() if o.is_open()]

    if not open_buy_orders:
        assert bt.reserved_fiat == 0, f"Reserved fiat={bt.reserved_fiat} but no open buy orders"
    if not open_sell_orders:
        assert bt.reserved_crypto == 0, f"Reserved crypto={bt.reserved_crypto} but no open sell orders"

    assert bt.get_adjusted_fiat_balance() >= 0, "Adjusted fiat balance is negative"
    assert bt.get_adjusted_crypto_balance() >= 0, "Adjusted crypto balance is negative"

    summary = result["performance_summary"]
    reported_final = float(summary["Final Balance (Fiat)"].split()[0])
    final_price = bot.strategy.close_prices[-1]
    computed_total = bt.get_total_balance_value(final_price)
    assert abs(computed_total - reported_final) < 0.02, (
        f"Balance mismatch: tracker says {computed_total:.4f}, summary says {reported_final:.4f}"
    )

    assert bot.strategy.data["account_value"].notna().all(), "Some candles have no account value"


@pytest.mark.integration
async def test_atr_scenario_value_change_splits_into_realized_and_unrealized(run_backtest_bot):
    """Trailing-stop regrids sell inventory: their losses must land in realized PnL."""
    bot, result = await _run_atr_scenario(run_backtest_bot)
    _assert_value_change_splits_into_realized_and_unrealized(bot, result)


@pytest.mark.integration
async def test_atr_scenario_deterministic_results(run_backtest_bot, snapshot_dir, update_snapshots):
    """With identical config and CSV data, the ATR scenario backtest produces identical results."""
    _bot, result = await _run_atr_scenario(run_backtest_bot)
    actual = extract_snapshot_data(result["performance_summary"], result["orders"])

    if update_snapshots:
        save_snapshot(ATR_SNAPSHOT_NAME, actual, snapshot_dir)
        pytest.skip(f"Snapshot updated for {ATR_SNAPSHOT_NAME}")

    expected = load_snapshot(ATR_SNAPSHOT_NAME, snapshot_dir)
    if expected is None:
        save_snapshot(ATR_SNAPSHOT_NAME, actual, snapshot_dir)
        pytest.skip(f"Snapshot created for {ATR_SNAPSHOT_NAME} (first run). Re-run to validate.")

    diffs = compare_snapshots(actual, expected)
    assert not diffs, f"Snapshot mismatch for {ATR_SNAPSHOT_NAME}:\n" + "\n".join(diffs)


# ---------------------------------------------------------------------------
# Stop-loss exit: position liquidated, account valued to the end
# ---------------------------------------------------------------------------

STOP_LOSS = {"stop_loss": {"enabled": True, "threshold": 155}}
STOP_LOSS_SNAPSHOT_NAME = "simple_grid_arithmetic_stop_loss"


@pytest.mark.integration
async def test_stop_loss_scenario_liquidates_and_values_to_the_end(run_backtest_bot):
    """The SOL fixture falls through 155 on Aug 2: the stop liquidates everything and the curve stays complete."""
    bot, result = await run_backtest_bot("simple_grid", "arithmetic", risk_management_overrides=STOP_LOSS)
    bt = bot.balance_tracker

    metrics = result["metrics"]
    assert metrics.stop_reason == "stop_loss"
    assert metrics.equity_coverage == 1.0
    assert bot.order_book.get_open_orders() == []
    assert bt.reserved_fiat == 0
    assert bt.reserved_crypto == 0
    assert bt.get_adjusted_crypto_balance() == pytest.approx(0.0, abs=1e-9)


@pytest.mark.integration
async def test_stop_loss_scenario_leaves_the_whole_result_realized(run_backtest_bot):
    """After the stop liquidates the position, the account value change is realized PnL only."""
    bot, result = await run_backtest_bot("simple_grid", "arithmetic", risk_management_overrides=STOP_LOSS)
    snapshot = _assert_value_change_splits_into_realized_and_unrealized(bot, result)

    assert snapshot.unrealized_pnl == pytest.approx(0.0, abs=1e-6)


@pytest.mark.integration
async def test_stop_loss_scenario_deterministic_results(run_backtest_bot, snapshot_dir, update_snapshots):
    """With identical config and CSV data, the stop-loss scenario produces identical results."""
    _bot, result = await run_backtest_bot("simple_grid", "arithmetic", risk_management_overrides=STOP_LOSS)
    actual = extract_snapshot_data(result["performance_summary"], result["orders"])

    if update_snapshots:
        save_snapshot(STOP_LOSS_SNAPSHOT_NAME, actual, snapshot_dir)
        pytest.skip(f"Snapshot updated for {STOP_LOSS_SNAPSHOT_NAME}")

    expected = load_snapshot(STOP_LOSS_SNAPSHOT_NAME, snapshot_dir)
    if expected is None:
        save_snapshot(STOP_LOSS_SNAPSHOT_NAME, actual, snapshot_dir)
        pytest.skip(f"Snapshot created for {STOP_LOSS_SNAPSHOT_NAME} (first run). Re-run to validate.")

    diffs = compare_snapshots(actual, expected)
    assert not diffs, f"Snapshot mismatch for {STOP_LOSS_SNAPSHOT_NAME}:\n" + "\n".join(diffs)
