from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_FEED_LOST = 75  # EX_TEMPFAIL: the supervisor should relaunch the process
EXIT_CONFIG = 78  # EX_CONFIG


class StopKind(Enum):
    MANUAL = "manual"
    RISK = "risk"
    INFEASIBLE = "infeasible"
    PERSISTENCE = "persistence"
    SIGNAL = "signal"
    FEED_LOST = "feed_lost"
    CRASH = "crash"

    @property
    def halts(self) -> bool:
        """Whether the stop is deliberate: open orders are cancelled and no relaunch trades again."""
        return self in {StopKind.MANUAL, StopKind.RISK, StopKind.INFEASIBLE, StopKind.PERSISTENCE}


@dataclass(frozen=True)
class StopRequest:
    kind: StopKind
    reason: str
    signal_number: int | None = None

    def __post_init__(self) -> None:
        if self.kind is StopKind.SIGNAL and self.signal_number is None:
            raise ValueError("A SIGNAL stop request needs its signal_number.")

    @property
    def exit_code(self) -> int:
        """Process exit code for this stop: 0 for a halt, non-zero when the supervisor should relaunch."""
        if self.kind.halts:
            return EXIT_OK
        if self.kind is StopKind.SIGNAL:
            return 128 + int(self.signal_number)
        if self.kind is StopKind.FEED_LOST:
            return EXIT_FEED_LOST
        return EXIT_FAILURE


def _severity(code: int) -> int:
    if code == EXIT_OK:
        return 0
    if code >= 128:
        return 1
    if code == EXIT_FEED_LOST:
        return 2
    return 3


def aggregate_exit_codes(codes: Iterable[int]) -> int:
    """Most severe exit code among several bots: a failure, then a lost feed, then a signal, then success."""
    return max(codes, key=_severity, default=EXIT_OK)
