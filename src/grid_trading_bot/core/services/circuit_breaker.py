import asyncio
from collections.abc import Callable, Coroutine
from enum import Enum
import logging
import time
from typing import Any

from grid_trading_bot.core.services.exceptions import CircuitBreakerOpenError


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, func: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            self._check_state_transition()

            if self._state == CircuitState.OPEN:
                raise CircuitBreakerOpenError(f"Circuit breaker is open. Retry after {self.recovery_timeout}s.")

            if self._state == CircuitState.HALF_OPEN and self._half_open_calls >= self.half_open_max_calls:
                raise CircuitBreakerOpenError("Circuit breaker half-open probe limit reached.")

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1

        try:
            result = await func(*args, **kwargs)
        except Exception:
            await self._on_failure()
            raise

        await self._on_success()
        return result

    def _check_state_transition(self) -> None:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self.logger.warning("Circuit breaker transitioning from OPEN to HALF_OPEN.")
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self.logger.warning("Circuit breaker transitioning from HALF_OPEN to CLOSED.")
                self._state = CircuitState.CLOSED
            self._failure_count = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self.logger.warning("Circuit breaker transitioning from HALF_OPEN to OPEN.")
                self._state = CircuitState.OPEN
            elif self._failure_count >= self.failure_threshold:
                self.logger.warning(
                    f"Circuit breaker transitioning from CLOSED to OPEN "
                    f"after {self._failure_count} consecutive failures."
                )
                self._state = CircuitState.OPEN
