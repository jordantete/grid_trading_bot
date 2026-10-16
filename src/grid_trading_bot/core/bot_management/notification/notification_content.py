from dataclasses import dataclass
from enum import Enum


@dataclass
class NotificationContent:
    title: str
    message: str


class NotificationType(Enum):
    ORDER_PLACED = NotificationContent(
        title="Order Placement Successful",
        message=("Order placed successfully:\n{order_details}"),
    )
    ORDER_FILLED = NotificationContent(
        title="Order Filled",
        message=("Order has been filled successfully:\n{order_details}"),
    )
    ORDER_FAILED = NotificationContent(
        title="Order Placement Failed",
        message=("Failed to place order:\n{error_details}"),
    )
    ORDER_CANCELLED = NotificationContent(
        title="Order Cancellation",
        message=("Order has been cancelled:\n{order_details}"),
    )
    ERROR_OCCURRED = NotificationContent(
        title="Error Occurred",
        message="An error occurred in the trading bot:\n{error_details}",
    )
    TAKE_PROFIT_TRIGGERED = NotificationContent(
        title="Take Profit Triggered",
        message="Take profit triggered with order details:\n{order_details}",
    )
    STOP_LOSS_TRIGGERED = NotificationContent(
        title="Stop Loss Triggered",
        message="Stop loss triggered with order details:\n{order_details}",
    )
    HEALTH_CHECK_ALERT = NotificationContent(
        title="Health Check Alert",
        message=("{alert_details}"),
    )
    RECONCILIATION_ORDER_MISMATCH = NotificationContent(
        title="Order Reconciliation Mismatch",
        message="Order reconciliation detected mismatches:\n{alert_details}",
    )
    RECONCILIATION_BALANCE_DRIFT = NotificationContent(
        title="Balance Reconciliation Drift",
        message="Balance reconciliation detected drift:\n{alert_details}",
    )
    STATE_RECOVERY_COMPLETE = NotificationContent(
        title="State Recovery Complete",
        message="Bot recovered from previous state:\n{recovery_details}",
    )
    BOT_HALTED = NotificationContent(
        title="Bot Halted",
        message=(
            "Trading halted ({kind}): {reason}\n"
            "Open orders were cancelled. The bot will not trade again until "
            "`grid_trading_bot resume --config {config_path}` is run."
        ),
    )
    BOT_SUSPENDED = NotificationContent(
        title="Bot Suspended",
        message="Bot stopped ({kind}): {reason}\nOpen orders were left on the exchange; the next start resumes them.",
    )
    START_REFUSED_HALTED = NotificationContent(
        title="Start Refused",
        message=(
            "Trading was halted on {halted_at} ({kind}): {reason}\n"
            "Run `grid_trading_bot resume --config {config_path}` to start a new grid."
        ),
    )
