import logging
import math


class TrailingStopLoss:
    """ATR-based trailing stop with a monotonic ratchet: the stop only moves up."""

    def __init__(self, atr_multiplier: float):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.atr_multiplier = atr_multiplier
        self.stop_price: float | None = None

    def update(self, close: float, atr: float) -> None:
        if math.isnan(atr) or atr <= 0:
            return
        candidate = close - self.atr_multiplier * atr
        if self.stop_price is None or candidate > self.stop_price:
            self.stop_price = candidate

    def is_triggered(self, price: float) -> bool:
        return self.stop_price is not None and price <= self.stop_price

    def reset(self) -> None:
        self.stop_price = None

    def to_dict(self) -> dict:
        return {"stop_price": self.stop_price, "atr_multiplier": self.atr_multiplier}

    @classmethod
    def from_dict(cls, data: dict) -> "TrailingStopLoss":
        instance = cls(atr_multiplier=data["atr_multiplier"])
        instance.stop_price = data.get("stop_price")
        return instance
