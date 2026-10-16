import contextlib
from dataclasses import asdict, dataclass
from datetime import datetime
import fcntl
import json
import os

from grid_trading_bot.config.config_manager import ConfigManager
from grid_trading_bot.core.persistence.serializers import compute_config_hash


@dataclass(frozen=True)
class RunFiles:
    db_path: str
    lock_path: str
    halt_path: str

    @classmethod
    def for_config(cls, config_manager: ConfigManager) -> "RunFiles":
        """Lock and halt marker paths of a config, next to its state database and keyed like it."""
        db_path = config_manager.get_state_db_path()
        directory = os.path.dirname(db_path) or "."
        config_hash_short = compute_config_hash(config_manager)[:8]
        return cls(
            db_path=db_path,
            lock_path=os.path.join(directory, f"bot_{config_hash_short}.lock"),
            halt_path=os.path.join(directory, f"halted_{config_hash_short}.json"),
        )


@dataclass(frozen=True)
class HaltRecord:
    kind: str
    reason: str
    pair: str
    halted_at: str


def write_halt_marker(path: str, record: HaltRecord) -> None:
    """Records a deliberate stop; written through a temporary file so a crash never leaves half a marker."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w") as marker_file:
        json.dump(asdict(record), marker_file)
    os.replace(temporary_path, path)


def read_halt_marker(path: str) -> HaltRecord | None:
    """The recorded halt, None without a marker. An unreadable marker still counts as a halt."""
    try:
        with open(path) as marker_file:
            return HaltRecord(**json.load(marker_file))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, TypeError):
        return HaltRecord(kind="unknown", reason=f"Unreadable halt marker {path}", pair="", halted_at="unknown")


def remove_halt_marker(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


class RunLock:
    """Exclusive lock held while a bot runs a config, so neither a second process nor `resume` touches its state."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


def archive_state_db(db_path: str, now: datetime) -> list[str]:
    """Moves the state database and its WAL side files aside so the next start is a fresh one."""
    suffix = now.strftime("%Y%m%dT%H%M%SZ")
    moved = []
    for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
        if os.path.exists(path):
            target = f"{path}.halted-{suffix}"
            os.replace(path, target)
            moved.append(target)
    return moved
