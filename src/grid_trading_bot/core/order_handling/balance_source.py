from enum import Enum


class BalanceSource(Enum):
    """Where the starting balances come from, and where they are restored from after a restart."""

    VIRTUAL = "virtual"
    EXCHANGE = "exchange"
