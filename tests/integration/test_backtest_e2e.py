"""End-to-end integration tests for backtest mode.

These tests run the full bot stack (config → exchange service → grid manager →
order manager → balance tracker → performance analyzer) with **zero mocks**.
Only the plotting is disabled.
"""

import pytest

from core.grid_management.grid_level import GridCycleState

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
    bot, result = await run_backtest_bot(strategy_type, spacing)

    assert result is not None, "run() returned None — backtest did not produce results"
    assert "performance_summary" in result
    assert "orders" in result

    summary = result["performance_summary"]
    expected_keys = {"ROI", "Total Fees", "Final Balance (Fiat)", "Number of Buy Trades", "Number of Sell Trades"}
    missing = expected_keys - set(summary.keys())
    assert not missing, f"Performance summary missing keys: {missing}"

    assert len(result["orders"]) > 0, "No trades were executed during backtest"


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
    assert (
        abs(computed_total - reported_final) < 0.02
    ), f"Balance mismatch: tracker says {computed_total:.4f}, summary says {reported_final:.4f}"


# ---------------------------------------------------------------------------
# V4: Grid cycle integrity
# ---------------------------------------------------------------------------

VALID_END_STATES_SIMPLE = {
    GridCycleState.READY_TO_BUY,
    GridCycleState.READY_TO_SELL,
    GridCycleState.WAITING_FOR_BUY_FILL,
    GridCycleState.WAITING_FOR_SELL_FILL,
}

VALID_END_STATES_HEDGED = VALID_END_STATES_SIMPLE | {GridCycleState.READY_TO_BUY_OR_SELL}


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_grid_cycle_integrity(strategy_type, spacing, run_backtest_bot):
    """All grid levels end in valid states, and pending orders have matching open orders."""
    bot, _ = await run_backtest_bot(strategy_type, spacing)
    grid_manager = bot.strategy.grid_manager
    order_book = bot.strategy.order_manager.order_book

    valid_states = VALID_END_STATES_HEDGED if "hedged" in strategy_type else VALID_END_STATES_SIMPLE

    open_buy_prices = {o.price for o in order_book.get_all_buy_orders() if o.is_open()}
    open_sell_prices = {o.price for o in order_book.get_all_sell_orders() if o.is_open()}

    for price, level in grid_manager.grid_levels.items():
        assert level.state in valid_states, f"Grid level {price} in invalid state: {level.state.name}"

        if level.state == GridCycleState.WAITING_FOR_BUY_FILL:
            assert (
                price in open_buy_prices
            ), f"Grid level {price} is WAITING_FOR_BUY_FILL but no open buy order exists at that price"

        if level.state == GridCycleState.WAITING_FOR_SELL_FILL:
            assert (
                price in open_sell_prices
            ), f"Grid level {price} is WAITING_FOR_SELL_FILL but no open sell order exists at that price"

    # Every filled buy should have had a corresponding sell placed (or the level is ready to sell)
    filled_buys = [o for o in order_book.get_all_buy_orders() if o.is_filled()]
    for buy_order in filled_buys:
        grid_level = order_book.get_grid_level_for_order(buy_order)
        if grid_level is None:
            continue  # initial purchase or non-grid order
        assert (
            grid_level.state != GridCycleState.READY_TO_BUY
        ), f"Buy filled at {grid_level.price} but level reverted to READY_TO_BUY without sell cycle"


# ---------------------------------------------------------------------------
# V3: Deterministic snapshot results
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("strategy_type", "spacing"), SCENARIOS)
async def test_backtest_deterministic_results(strategy_type, spacing, run_backtest_bot, snapshot_dir, update_snapshots):
    """With identical config and CSV data, the backtest produces identical results."""
    bot, result = await run_backtest_bot(strategy_type, spacing)
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
