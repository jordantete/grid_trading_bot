from enum import Enum


class SpacingType(Enum):
    ARITHMETIC = "arithmetic"
    GEOMETRIC = "geometric"

    @staticmethod
    def from_string(spacing_type_str: str):
        try:
            return SpacingType(spacing_type_str)
        except ValueError:
            available_spacings = ", ".join([spacing.value for spacing in SpacingType])
            raise ValueError(
                f"Invalid spacing type: '{spacing_type_str}'. Available spacings are: {available_spacings}",
            ) from None
