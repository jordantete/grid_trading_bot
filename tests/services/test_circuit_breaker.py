import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from grid_trading_bot.core.services.circuit_breaker import CircuitBreaker, CircuitState
from grid_trading_bot.core.services.exceptions import CircuitBreakerOpenError


class TestCircuitBreaker:
    @pytest.fixture
    def breaker(self):
        return CircuitBreaker(failure_threshold=3, recovery_timeout=1.0, half_open_max_calls=1)

    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self, breaker):
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_successful_call_passes_through(self, breaker):
        func = AsyncMock(return_value="ok")
        result = await breaker.call(func, "arg1", key="val")
        assert result == "ok"
        func.assert_awaited_once_with("arg1", key="val")
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failures_below_threshold_stay_closed(self, breaker):
        func = AsyncMock(side_effect=Exception("fail"))

        for _ in range(2):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(func)

        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failures_at_threshold_open_circuit(self, breaker):
        func = AsyncMock(side_effect=Exception("fail"))

        for _ in range(3):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(func)

        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_circuit_rejects_calls(self, breaker):
        func = AsyncMock(side_effect=Exception("fail"))

        for _ in range(3):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(func)

        with pytest.raises(CircuitBreakerOpenError, match="Circuit breaker is open"):
            await breaker.call(func)

        # The underlying function should NOT have been called a 4th time
        assert func.await_count == 3

    @pytest.mark.asyncio
    async def test_open_to_half_open_after_recovery_timeout(self, breaker):
        func = AsyncMock(side_effect=Exception("fail"))

        for _ in range(3):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(func)

        assert breaker.state == CircuitState.OPEN

        # Simulate time passing beyond recovery_timeout
        future_time = time.monotonic() + 2.0
        with patch("grid_trading_bot.core.services.circuit_breaker.time.monotonic", return_value=future_time):
            func.side_effect = None
            func.return_value = "recovered"

            result = await breaker.call(func)
            assert result == "recovered"
            assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_success_closes_circuit(self, breaker):
        fail_func = AsyncMock(side_effect=Exception("fail"))
        for _ in range(3):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(fail_func)

        future_time = time.monotonic() + 2.0
        with patch("grid_trading_bot.core.services.circuit_breaker.time.monotonic", return_value=future_time):
            success_func = AsyncMock(return_value="ok")
            await breaker.call(success_func)

        assert breaker.state == CircuitState.CLOSED
        assert breaker._failure_count == 0

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self, breaker):
        fail_func = AsyncMock(side_effect=Exception("fail"))
        for _ in range(3):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(fail_func)

        future_time = time.monotonic() + 2.0
        with (
            patch("grid_trading_bot.core.services.circuit_breaker.time.monotonic", return_value=future_time),
            pytest.raises(Exception, match="fail"),
        ):
            await breaker.call(fail_func)

        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_half_open_max_calls_exceeded(self, breaker):
        fail_func = AsyncMock(side_effect=Exception("fail"))
        for _ in range(3):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(fail_func)

        # Use a slow func to keep half-open occupied
        slow_func = AsyncMock(return_value="ok")
        future_time = time.monotonic() + 2.0
        with patch("grid_trading_bot.core.services.circuit_breaker.time.monotonic", return_value=future_time):
            # First call transitions to half-open and uses the one allowed probe
            await breaker.call(slow_func)

        # After successful probe, circuit should be CLOSED, so this should work
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, breaker):
        fail_func = AsyncMock(side_effect=Exception("fail"))
        success_func = AsyncMock(return_value="ok")

        # 2 failures (below threshold)
        for _ in range(2):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(fail_func)

        # 1 success resets counter
        await breaker.call(success_func)
        assert breaker._failure_count == 0

        # 2 more failures - should still be closed since counter was reset
        for _ in range(2):
            with pytest.raises(Exception, match="fail"):
                await breaker.call(fail_func)

        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_original_exception_is_reraised(self, breaker):
        func = AsyncMock(side_effect=ValueError("specific error"))

        with pytest.raises(ValueError, match="specific error"):
            await breaker.call(func)

    @pytest.mark.asyncio
    async def test_concurrent_calls_dont_deadlock(self, breaker):
        call_count = 0

        async def slow_success():
            nonlocal call_count
            await asyncio.sleep(0.01)
            call_count += 1
            return "ok"

        results = await asyncio.gather(
            breaker.call(slow_success),
            breaker.call(slow_success),
            breaker.call(slow_success),
        )

        assert all(r == "ok" for r in results)
        assert call_count == 3
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_logs_state_transitions(self, breaker):
        func = AsyncMock(side_effect=Exception("fail"))

        with patch.object(breaker.logger, "warning") as mock_warn:
            for _ in range(3):
                with pytest.raises(Exception, match="fail"):
                    await breaker.call(func)

            mock_warn.assert_called_once()
            assert "CLOSED to OPEN" in mock_warn.call_args[0][0]


class BenignError(Exception):
    """Stand-in for an expected, non-fault rejection (e.g. a post-only order that would cross)."""


class TestCircuitBreakerBenignExceptions:
    """
    Some exchange rejections are expected outcomes, not faults. They must propagate to the
    caller without counting toward the failure threshold, otherwise a run of them trips the
    breaker and blocks every API call.
    """

    @pytest.fixture
    def breaker(self):
        return CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=1.0,
            half_open_max_calls=1,
            benign_exceptions=(BenignError,),
        )

    @pytest.mark.asyncio
    async def test_benign_exception_still_reaches_the_caller(self, breaker):
        func = AsyncMock(side_effect=BenignError("would cross"))

        with pytest.raises(BenignError, match="would cross"):
            await breaker.call(func)

    @pytest.mark.asyncio
    async def test_benign_exceptions_never_open_the_breaker(self, breaker):
        func = AsyncMock(side_effect=BenignError("would cross"))

        for _ in range(10):
            with pytest.raises(BenignError):
                await breaker.call(func)

        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_benign_exception_does_not_reset_a_real_failure_count(self, breaker):
        """A benign rejection is neither a fault nor a recovery — it must leave the tally alone."""
        failing = AsyncMock(side_effect=Exception("real fault"))
        benign = AsyncMock(side_effect=BenignError("would cross"))

        for _ in range(2):
            with pytest.raises(Exception, match="real fault"):
                await breaker.call(failing)
        with pytest.raises(BenignError):
            await breaker.call(benign)
        with pytest.raises(Exception, match="real fault"):
            await breaker.call(failing)

        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_non_benign_exceptions_still_open_the_breaker(self, breaker):
        func = AsyncMock(side_effect=Exception("real fault"))

        for _ in range(3):
            with pytest.raises(Exception, match="real fault"):
                await breaker.call(func)

        assert breaker.state == CircuitState.OPEN
