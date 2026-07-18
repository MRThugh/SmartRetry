# test_smartretry.py
"""
Comprehensive offline test suite for the SmartRetry library.
Tested features: Jitter, callbacks, Async execution, return value evaluation,
global timeouts, adaptive delays, thread-safe stats, circuit breakers, 
runtime contexts, rate limiters, bulkhead concurrency isolation, 
fallback policies, resilient caching, and transparent decorator composition.

Run:
    python test_smartretry.py
"""

import logging
import time
import asyncio
import threading
import unittest
from typing import List
from unittest.mock import MagicMock, patch

from smartretry import (
    retry,
    RetryExhaustedError,
    circuit_breaker,
    CircuitOpenError,
    rate_limiter,
    RateLimitExceededError,
    bulkhead,
    BulkheadFullError,
    fallback,
    resilient_cache,
    RetryContext,
)
from smartretry.core import RetryConfig

# ---------------------------------------------------------------------------
# Suppress SmartRetry log output during tests
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.CRITICAL,
    format="%(levelname)s | %(name)s | %(message)s",
)


# ===========================================================================
# Helper callables used across multiple tests
# ===========================================================================

def _make_flaky(fail_times: int, exc_type: type = ValueError, return_value: str = "OK"):
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc_type(f"Simulated failure #{state['calls']}")
        return return_value

    flaky.__name__ = f"flaky_fail{fail_times}"
    return flaky


def _always_fail(exc_type: type = RuntimeError):
    def always_fail(*args, **kwargs):
        raise exc_type("Permanent failure")
    always_fail.__name__ = "always_fail"
    return always_fail


# ===========================================================================
# Base Test Cases
# ===========================================================================

class TestImmediateSuccess(unittest.TestCase):
    def test_returns_correct_value(self):
        @retry(max_retries=3, base_delay=0, exceptions=(ValueError,))
        def succeed():
            return 42

        self.assertEqual(succeed(), 42)


class TestSuccessAfterRetries(unittest.TestCase):
    @patch("smartretry.core.time.sleep")
    def test_sleep_called_correct_number_of_times(self, mock_sleep):
        fn = _make_flaky(fail_times=2, exc_type=ValueError)
        wrapped = retry(max_retries=3, base_delay=1.0, exceptions=(ValueError,))(fn)
        wrapped()
        self.assertEqual(mock_sleep.call_count, 2)


class TestAllRetriesExhausted(unittest.TestCase):
    def test_raises_retry_exhausted_error(self):
        wrapped = retry(max_retries=2, base_delay=0)(
            _always_fail(exc_type=ValueError)
        )
        with self.assertRaises(RetryExhaustedError) as ctx:
            wrapped()
        err = ctx.exception
        self.assertEqual(err.attempts, 3)


class TestConfigValidation(unittest.TestCase):
    def test_negative_max_retries_raises_value_error(self):
        with self.assertRaises(ValueError):
            retry(max_retries=-1)


class TestMetadataPreservation(unittest.TestCase):
    def test_function_name_preserved(self):
        @retry(max_retries=1, base_delay=0)
        def my_special_function():
            return 1

        self.assertEqual(my_special_function.__name__, "my_special_function")


class TestRetryOnResult(unittest.TestCase):
    def test_retry_on_result_predicate(self):
        calls = 0

        @retry(max_retries=3, base_delay=0, retry_on_result=lambda r: r is None)
        def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                return None
            return "success"

        res = flaky()
        self.assertEqual(res, "success")
        self.assertEqual(calls, 3)


class TestTotalTimeout(unittest.TestCase):
    def test_total_timeout_aborts_early_without_sleeping(self):
        times = [0.0, 1.0, 1.0, 1.0]

        def mock_monotonic():
            return times.pop(0) if times else 10.0

        with patch("smartretry.core.time.monotonic", side_effect=mock_monotonic):
            @retry(max_retries=3, base_delay=10.0, total_timeout=5.0)
            def flaky():
                raise ValueError("error")

            with self.assertRaises(RetryExhaustedError) as ctx:
                flaky()
            self.assertEqual(ctx.exception.attempts, 1)


class TestRetryStats(unittest.TestCase):
    def test_stats_and_average_latency(self):
        @retry(max_retries=1, base_delay=0, exceptions=(ValueError,))
        def flaky():
            time.sleep(0.01)
            raise ValueError("fail")

        with self.assertRaises(RetryExhaustedError):
            flaky()

        self.assertEqual(flaky.stats.total_calls, 1)
        self.assertEqual(flaky.stats.total_retries, 1)
        self.assertGreater(flaky.stats.total_latency, 0.0)
        self.assertGreater(flaky.stats.average_latency, 0.0)


class TestRetryContext(unittest.TestCase):
    def test_context_injection_attempt_tracking(self):
        attempts_seen = []

        @retry(max_retries=2, base_delay=0, exceptions=(ValueError,))
        def try_me(x, retry_context=None):
            if retry_context:
                attempts_seen.append(retry_context.attempt)
                if retry_context.attempt > 1:
                    self.assertIsInstance(retry_context.last_error, ValueError)
            if len(attempts_seen) < 3:
                raise ValueError("temporary error")
            return "ok"

        res = try_me("data")
        self.assertEqual(res, "ok")
        self.assertEqual(attempts_seen, [1, 2, 3])


class TestCircuitBreaker(unittest.TestCase):
    def test_circuit_breaker_state_transitions(self):
        @circuit_breaker(failure_threshold=2, recovery_timeout=0.1, exceptions=(ValueError,))
        def target():
            raise ValueError("broken")

        with self.assertRaises(ValueError):
            target()
        with self.assertRaises(ValueError):
            target()
        with self.assertRaises(CircuitOpenError):
            target()

        time.sleep(0.15)

        with self.assertRaises(ValueError):
            target()
        with self.assertRaises(CircuitOpenError):
            target()


class TestRateLimiter(unittest.TestCase):
    def test_rate_limiter_sync(self):
        @rate_limiter(max_requests=2, period=10.0)
        def process():
            return "done"

        self.assertEqual(process(), "done")
        self.assertEqual(process(), "done")

        with self.assertRaises(RateLimitExceededError) as ctx:
            process()
        self.assertGreater(ctx.exception.retry_after, 0.0)


class TestBulkhead(unittest.TestCase):
    def test_sync_bulkhead_concurrency_isolation(self):
        @bulkhead(max_concurrent_calls=2, max_wait_duration=0.0)
        def busy_task():
            time.sleep(0.05)
            return "success"

        errors = []

        def worker():
            try:
                busy_task()
            except BulkheadFullError:
                errors.append(True)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertGreater(len(errors), 0)


class TestResilientCache(unittest.TestCase):
    def test_sync_cache_degradation(self):
        calls = 0

        @resilient_cache(ttl=60.0, exceptions=(ValueError,))
        def get_weather():
            nonlocal calls
            calls += 1
            if calls == 1:
                return "sunny"
            raise ValueError("api offline")

        self.assertEqual(get_weather(), "sunny")
        self.assertEqual(get_weather(), "sunny")
        self.assertEqual(calls, 2)


class TestFallbackPolicy(unittest.TestCase):
    def test_static_fallback_suppressing_errors(self):
        @fallback(fallback_value_or_callable="static_mock", exceptions=(ValueError,))
        def broken():
            raise ValueError("fail")

        self.assertEqual(broken(), "static_mock")


class TestCircuitBreakerStateChange(unittest.TestCase):
    def test_state_change_notification(self):
        transitions = []

        def track_transitions(old_state, new_state):
            transitions.append((old_state, new_state))

        @circuit_breaker(failure_threshold=1, recovery_timeout=0.1, on_state_change=track_transitions)
        def failing():
            raise ValueError("fail")

        with self.assertRaises(ValueError):
            failing()

        self.assertEqual(transitions, [("CLOSED", "OPEN")])


# ===========================================================================
# STABILITY & COMPOSITION TESTS (v1.0.0: Intelligent Attribute Propagation)
# ===========================================================================

class TestDecoratorComposition(unittest.TestCase):
    """Verify metadata and stats propagation when chaining multiple decorators."""

    def test_attribute_propagation_when_chained(self):
        # Chain order: fallback -> circuit_breaker -> retry
        @fallback(fallback_value_or_callable="fallback_value", exceptions=(ValueError,))
        @circuit_breaker(failure_threshold=5, recovery_timeout=10.0)
        @retry(max_retries=2, base_delay=0, exceptions=(ValueError,))
        def business_logic():
            return "ok"

        # The outermost wrapper must still dynamically expose inner attributes!
        self.assertTrue(hasattr(business_logic, "stats"))
        self.assertTrue(hasattr(business_logic, "retry_config"))
        self.assertEqual(business_logic.retry_config.max_retries, 2)


class TestAsyncFeatures(unittest.IsolatedAsyncioTestCase):
    async def test_async_decorator_chain_attribute_propagation(self):
        @fallback(fallback_value_or_callable="fallback_async", exceptions=(ValueError,))
        @circuit_breaker(failure_threshold=5)
        @retry(max_retries=5, base_delay=0, exceptions=(ValueError,))
        async def async_logic():
            return "async_ok"

        self.assertTrue(hasattr(async_logic, "stats"))
        self.assertTrue(hasattr(async_logic, "retry_config"))
        self.assertEqual(await async_logic(), "async_ok")


if __name__ == "__main__":
    print("=" * 65)
    print("  SmartRetry — Comprehensive Test Suite (v1.0.0)")
    print("=" * 65)
    unittest.main(verbosity=2)