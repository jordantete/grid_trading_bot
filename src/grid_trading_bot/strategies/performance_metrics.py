from dataclasses import dataclass


@dataclass(frozen=True)
class PerformanceMetrics:
    """Numeric results of a trading session; the display summary is formatted from them."""

    roi_pct: float
    max_drawdown_pct: float
    max_drawdown_duration_hours: float
    sharpe_ratio: float
    sortino_ratio: float
    buy_and_hold_return_pct: float
    grid_trading_gains: float | None
    unrealized_pnl: float
    total_fees: float
    final_balance: float
    num_buy_trades: int
    num_sell_trades: int
    avg_crypto_exposure: float | None
    equity_coverage: float
    armed: bool
    stop_reason: str | None
