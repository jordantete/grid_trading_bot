from enum import Enum


class StrategyType(Enum):
    SIMPLE_GRID = "simple_grid"
    HEDGED_GRID = "hedged_grid"

    @staticmethod
    def from_string(strategy_type_str: str):
        try:
            return StrategyType(strategy_type_str)
        except ValueError:
            available_strategies = ", ".join([strat.value for strat in StrategyType])
            raise ValueError(
                f"Invalid strategy type: '{strategy_type_str}'. Available strategies are: {available_strategies}",
            ) from None
