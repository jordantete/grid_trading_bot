import hashlib
import json
from typing import Any

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.grid_management.grid_level import GridLevel
from grid_trading_bot.core.order_handling.balance_tracker import BalanceTracker
from grid_trading_bot.core.order_handling.order import Order, OrderSide, OrderStatus, OrderType


def order_to_dict(order: Order, grid_level_price: float | None, is_non_grid: bool) -> dict[str, Any]:
    return {
        "identifier": order.identifier,
        "status": order.status.value,
        "order_type": order.order_type.value,
        "side": order.side.value,
        "price": order.price,
        "average": order.average,
        "amount": order.amount,
        "filled": order.filled,
        "remaining": order.remaining,
        "timestamp": order.timestamp,
        "datetime_str": order.datetime,
        "last_trade_timestamp": order.last_trade_timestamp,
        "symbol": order.symbol,
        "time_in_force": order.time_in_force,
        "cost": order.cost,
        "trades_json": json.dumps(order.trades) if order.trades else None,
        "fee_json": json.dumps(order.fee) if order.fee else None,
        "info_json": json.dumps(order.info) if order.info else None,
        "grid_level_price": grid_level_price,
        "is_non_grid_order": 1 if is_non_grid else 0,
    }


def dict_to_order(row: dict[str, Any]) -> Order:
    return Order(
        identifier=row["identifier"],
        status=OrderStatus(row["status"]),
        order_type=OrderType(row["order_type"]),
        side=OrderSide(row["side"]),
        price=row["price"],
        average=row.get("average"),
        amount=row["amount"],
        filled=row.get("filled", 0.0),
        remaining=row["remaining"],
        timestamp=row["timestamp"],
        datetime=row.get("datetime_str"),
        last_trade_timestamp=row.get("last_trade_timestamp"),
        symbol=row["symbol"],
        time_in_force=row.get("time_in_force"),
        cost=row.get("cost"),
        trades=json.loads(row["trades_json"]) if row.get("trades_json") else None,
        fee=json.loads(row["fee_json"]) if row.get("fee_json") else None,
        info=json.loads(row["info_json"]) if row.get("info_json") else None,
    )


def grid_level_to_dict(grid_level: GridLevel) -> dict[str, Any]:
    return {
        "price": grid_level.price,
        "state": grid_level.state.value,
        "paired_buy_level_price": grid_level.paired_buy_level.price if grid_level.paired_buy_level else None,
        "paired_sell_level_price": grid_level.paired_sell_level.price if grid_level.paired_sell_level else None,
    }


def balance_to_dict(tracker: BalanceTracker) -> dict[str, str]:
    return {
        "fiat_balance": str(tracker._balance),
        "crypto_balance": str(tracker._crypto_balance),
        "total_fees": str(tracker._total_fees),
        "reserved_fiat": str(tracker._reserved_fiat),
        "reserved_crypto": str(tracker._reserved_crypto),
    }


def compute_config_hash(config_manager: ConfigManager) -> str:
    grid_settings = config_manager.get_grid_settings()
    pair = config_manager.get_pair()
    hash_input = {
        "strategy_type": grid_settings.get("type"),
        "spacing": grid_settings.get("spacing"),
        "num_grids": grid_settings.get("num_grids"),
        "range": grid_settings.get("range"),
        "pair": pair,
    }
    canonical = json.dumps(hash_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
