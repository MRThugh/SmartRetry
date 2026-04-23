# test_smartretry.py
"""
Comprehensive offline test suite for the SmartRetry library.

Test Categories
---------------
1.  Immediate success          — function works on the first call.
2.  Success after N retries    — function fails N times, then succeeds.
3.  All retries exhausted      — RetryExhaustedError is raised.
4.  Fallback activation        — fallback is called when retries run out.
5.  Non-retryable exception    — wrong exception type → re-raised instantly.
6.  No delay on first attempt  — time.sleep is never called for attempt #0.
7.  Delay formula              — verify $delay = base\_delay × backoff\_factor^{attempt}$.
8.  Config validation          — bad arguments raise TypeError / ValueError.
9.  Metadata preservation      — __name__, __doc__, __wrapped__ intact.
10. Fallback receives args     — fallback gets the same args as the original.
11. Logger integration         — correct log levels are emitted.
12. max_retries=0              — only one attempt, no retries.
13. base_delay=0               — no sleep even on retries.
14. Retry on multiple types    — multiple exception types in the tuple.
15. Random-failure simulation  — statistical test over many runs.

Run:
    python test_smartretry.py
"""

import logging
import time
import unittest
from typing import List
from unittest.mock import MagicMock, patch, call

from smartretry import retry, RetryExhaustedError
from smartretry.core import RetryConfig, _execute_with_retry

# ---------------------------------------------------------------------------
# Suppress SmartRetry log output during tests (set to DEBUG to see them).
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.CRITICAL,
    format="%(levelname)s | %(name)s | %(message)s",
)


# ===========================================================================
# Helper callables used across multiple tests
# ===========================================================================

def _make_flaky(fail_times: int, exc_type: type = ValueError, return_value: str = "OK"):
    """
    Return a callable that raises *exc_type* for the first *fail_times*
    calls, then returns *return_value* on every subsequent call.
    """
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc_type(f"Simulated failure #{state['calls']}")
        return return_value

    flaky.__name__ = f"flaky_fail{fail_times}"
    return flaky


def _always_fail(exc_type: type = RuntimeError):
    """Return a callable that always raises *exc_type*."""
    def always_fail(*args, **kwargs):
        raise exc_type("Permanent failure")
    always_fail.__name__ = "always_fail"
    return always_fail


# ===========================================================================
# Test Cases
# ===========================================================================

class TestImmediateSuccess(unittest.TestCase):
    """Function succeeds on the very first call."""

    def test_returns_correct_value(self):
        @retry(max_retries=3, base_delay=0, exceptions=(ValueError,))
        def succeed():
            return 42

        self.assertEqual(succeed(), 42)

    def test_called_exactly_once(self):
        mock_fn = MagicMock(return_value="hello")
        wrapped = retry(max_retries=5, base_delay=0)(mock_fn)
        result = wrapped()
        self.assertEqual(result, "hello")
        mock_fn.assert_called_once()


class TestSuccessAfterRetries(unittest.TestCase):
    """Function fails a predictable number of times, then succeeds."""

    def test_success_on_second_attempt(self):
        fn = _make_flaky(fail_times=1, exc_type=ValueError)
        wrapped = retry(max_retries=3, base_delay=0, exceptions=(ValueError,))(fn)
        self.assertEqual(wrapped(), "OK")

    def test_success_on_last_possible_attempt(self):
        fn = _make_flaky(fail_times=3, exc_type=OSError)
        wrapped = retry(max_retries=3, base_delay=0, exceptions=(OSError,))(fn)
        self.assertEqual(wrapped(), "OK")

    @patch("smartretry.core.time.sleep")
    def test_sleep_called_correct_number_of_times(self, mock_sleep):
        """sleep() is called once per retry (not on the initial attempt)."""
        fn = _make_flaky(fail_times=2, exc_type=ValueError)
        wrapped = retry(max_retries=3, base_delay=1.0, exceptions=(ValueError,))(fn)
        wrapped()
        self.assertEqual(mock_sleep.call_count, 2)


class TestAllRetriesExhausted(unittest.TestCase):
    """RetryExhaustedError is raised when every attempt fails."""

    def test_raises_retry_exhausted_error(self):
        wrapped = retry(max_retries=2, base_delay=0)(
            _always_fail(exc_type=ValueError)
        )
        with self.assertRaises(RetryExhaustedError) as ctx:
            wrapped()
        err = ctx.exception
        self.assertEqual(err.attempts, 3)          # 1 initial + 2 retries
        self.assertIsInstance(err.last_error, ValueError)

    def test_error_message_contains_func_name(self):
        @retry(max_retries=1, base_delay=0, exceptions=(RuntimeError,))
        def target():
            raise RuntimeError("boom")

        with self.assertRaises(RetryExhaustedError) as ctx:
            target()
        self.assertIn("target", str(ctx.exception))

    def test_total_call_count_equals_max_retries_plus_one(self):
        counter = {"n": 0}

        @retry(max_retries=4, base_delay=0, exceptions=(Exception,))
        def count_calls():
            counter["n"] += 1
            raise Exception("fail")

        with self.assertRaises(RetryExhaustedError):
            count_calls()
        self.assertEqual(counter["n"], 5)


class TestFallbackMechanism(unittest.TestCase):
    """Fallback is invoked when all retries are exhausted."""

    def test_fallback_return_value_is_used(self):
        def my_fallback(*a, **kw):
            return "fallback_result"

        wrapped = retry(max_retries=2, base_delay=0, fallback=my_fallback)(
            _always_fail(exc_type=ValueError)
        )
        self.assertEqual(wrapped(), "fallback_result")

    def test_fallback_receives_original_arguments(self):
        received: List = []

        def capture_fallback(*args, **kwargs):
            received.extend(args)
            received.append(kwargs)
            return "captured"

        @retry(max_retries=1, base_delay=0, fallback=capture_fallback)
        def fn(x, y, z=99):
            raise IOError("nope")

        result = fn(1, 2, z=3)
        self.assertEqual(result, "captured")
        self.assertEqual(received, [1, 2, {"z": 3}])

    def test_no_retry_exhausted_error_when_fallback_present(self):
        @retry(max_retries=1, base_delay=0, fallback=lambda: "safe")
        def broken():
            raise RuntimeError("oops")

        # Should NOT raise — fallback handles it.
        result = broken()
        self.assertEqual(result, "safe")


class TestNonRetryableException(unittest.TestCase):
    """Exceptions not listed in `exceptions` must be re-raised at once."""

    def test_non_retryable_exception_propagates_immediately(self):
        call_count = {"n": 0}

        @retry(max_retries=5, base_delay=0, exceptions=(ValueError,))
        def fn():
            call_count["n"] += 1
            raise TypeError("wrong type")  # NOT in exceptions tuple

        with self.assertRaises(TypeError):
            fn()
        # Must have been called exactly ONCE — no retries.
        self.assertEqual(call_count["n"], 1)

    def test_correct_exception_type_still_retries(self):
        fn = _make_flaky(fail_times=2, exc_type=ValueError)
        wrapped = retry(max_retries=5, base_delay=0, exceptions=(ValueError,))(fn)
        result = wrapped()
        self.assertEqual(result, "OK")


class TestDelayFormula(unittest.TestCase):
    """Verify the delay calculation: $delay = base\\_delay × backoff\\_factor^{attempt}$"""

    @patch("smartretry.core.time.sleep")
    def test_delay_values_are_correct(self, mock_sleep):
        """
        With base_delay=1.0 and backoff_factor=3.0:
          Retry 0 (attempt index 0): 1.0 × 3^0 = 1.0 s
          Retry 1 (attempt index 1): 1.0 × 3^1 = 3.0 s
          Retry 2 (attempt index 2): 1.0 × 3^2 = 9.0 s
        """
        fn = _always_fail(exc_type=OSError)
        wrapped = retry(
            max_retries=3,
            base_delay=1.0,
            backoff_factor=3.0,
            exceptions=(OSError,),
        )(fn)

        with self.assertRaises(RetryExhaustedError):
            wrapped()

        expected_delays = [1.0, 3.0, 9.0]  # 3^0, 3^1, 3^2
        actual_delays = [c.args[0] for c in mock_sleep.call_args_list]
        for expected, actual in zip(expected_delays, actual_delays):
            self.assertAlmostEqual(actual, expected, places=9)

    def test_compute_delay_method_directly(self):
        cfg = RetryConfig(5, 2.0, 3.0, (Exception,), None, None)
        self.assertAlmostEqual(cfg.compute_delay(0), 2.0)   # 2 × 3^0
        self.assertAlmostEqual(cfg.compute_delay(1), 6.0)   # 2 × 3^1
        self.assertAlmostEqual(cfg.compute_delay(2), 18.0)  # 2 × 3^2
        self.assertAlmostEqual(cfg.compute_delay(3), 54.0)  # 2 × 3^3

    @patch("smartretry.core.time.sleep")
    def test_no_sleep_on_first_attempt(self, mock_sleep):
        @retry(max_retries=3, base_delay=1.0, exceptions=(Exception,))
        def succeed():
            return True

        succeed()
        mock_sleep.assert_not_called()


class TestConfigValidation(unittest.TestCase):
    """RetryConfig and retry() must validate inputs at decoration time."""

    def test_negative_max_retries_raises_value_error(self):
        with self.assertRaises(ValueError):
            retry(max_retries=-1)

    def test_negative_base_delay_raises_value_error(self):
        with self.assertRaises(ValueError):
            retry(base_delay=-0.1)

    def test_backoff_factor_below_one_raises_value_error(self):
        with self.assertRaises(ValueError):
            retry(backoff_factor=0.5)

    def test_non_int_max_retries_raises_type_error(self):
        with self.assertRaises(TypeError):
            retry(max_retries="three")  # type: ignore

    def test_non_tuple_exceptions_raises_type_error(self):
        with self.assertRaises(TypeError):
            retry(exceptions=ValueError)  # type: ignore

    def test_non_exception_in_exceptions_tuple_raises_type_error(self):
        with self.assertRaises(TypeError):
            retry(exceptions=(int,))  # int is not an Exception subclass

    def test_non_callable_fallback_raises_type_error(self):
        with self.assertRaises(TypeError):
            retry(fallback="not_callable")  # type: ignore

    def test_invalid_logger_raises_type_error(self):
        with self.assertRaises(TypeError):
            retry(logger="my_logger")  # type: ignore

    def test_zero_max_retries_is_valid(self):
        @retry(max_retries=0, base_delay=0)
        def fn():
            return "ok"
        self.assertEqual(fn(), "ok")

    def test_zero_base_delay_is_valid(self):
        @retry(max_retries=2, base_delay=0)
        def fn():
            return "ok"
        self.assertEqual(fn(), "ok")


class TestMetadataPreservation(unittest.TestCase):
    """functools.wraps must keep the original function's metadata intact."""

    def test_function_name_preserved(self):
        @retry(max_retries=1, base_delay=0)
        def my_special_function():
            """My docstring."""
            return 1

        self.assertEqual(my_special_function.__name__, "my_special_function")

    def test_docstring_preserved(self):
        @retry(max_retries=1, base_delay=0)
        def documented():
            """This is important documentation."""
            pass

        self.assertEqual(documented.__doc__, "This is important documentation.")

    def test_retry_config_exposed_on_wrapper(self):
        @retry(max_retries=7, base_delay=0.3, backoff_factor=1.5)
        def fn():
            pass

        cfg = fn.retry_config
        self.assertIsInstance(cfg, RetryConfig)
        self.assertEqual(cfg.max_retries, 7)
        self.assertAlmostEqual(cfg.base_delay, 0.3)
        self.assertAlmostEqual(cfg.backoff_factor, 1.5)


class TestMaxRetriesZero(unittest.TestCase):
    """With max_retries=0 only a single attempt is made."""

    def test_no_retry_on_failure(self):
        call_count = {"n": 0}

        @retry(max_retries=0, base_delay=0, exceptions=(Exception,))
        def fn():
            call_count["n"] += 1
            raise Exception("fail")

        with self.assertRaises(RetryExhaustedError) as ctx:
            fn()
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(ctx.exception.attempts, 1)

    def test_success_still_works(self):
        @retry(max_retries=0, base_delay=0)
        def fn():
            return "single shot"

        self.assertEqual(fn(), "single shot")


class TestMultipleExceptionTypes(unittest.TestCase):
    """Multiple exception types in the tuple should all trigger retries."""

    def test_both_types_trigger_retry(self):
        results: List[str] = []

        exc_sequence = [IOError("io"), ValueError("val"), None]
        idx = {"i": 0}

        @retry(max_retries=5, base_delay=0, exceptions=(IOError, ValueError))
        def fn():
            exc = exc_sequence[idx["i"]]
            idx["i"] += 1
            if exc is not None:
                raise exc
            return "success"

        result = fn()
        self.assertEqual(result, "success")

    def test_third_unrelated_type_not_retried(self):
        call_count = {"n": 0}

        @retry(max_retries=5, base_delay=0, exceptions=(IOError, ValueError))
        def fn():
            call_count["n"] += 1
            raise KeyError("unrelated")

        with self.assertRaises(KeyError):
            fn()
        self.assertEqual(call_count["n"], 1)


class TestLoggerIntegration(unittest.TestCase):
    """Verify that warnings and errors are emitted at correct log levels."""

    def test_warning_logged_on_retry(self):
        mock_log = MagicMock(spec=logging.Logger)

        fn = _make_flaky(fail_times=1, exc_type=ValueError)
        wrapped = retry(
            max_retries=3,
            base_delay=0,
            exceptions=(ValueError,),
            logger=mock_log,
        )(fn)
        wrapped()

        # At least one warning should have been emitted for the failed attempt.
        self.assertTrue(mock_log.warning.called)

    def test_error_logged_when_all_fail(self):
        mock_log = MagicMock(spec=logging.Logger)

        wrapped = retry(
            max_retries=1,
            base_delay=0,
            exceptions=(ValueError,),
            logger=mock_log,
        )(_always_fail(ValueError))

        with self.assertRaises(RetryExhaustedError):
            wrapped()

        self.assertTrue(mock_log.error.called)

    def test_custom_logger_instance_used(self):
        custom = logging.getLogger("test.custom")
        with self.assertLogs("test.custom", level="WARNING") as log_ctx:
            @retry(max_retries=1, base_delay=0, exceptions=(OSError,), logger=custom)
            def broken():
                raise OSError("fail")

            with self.assertRaises(RetryExhaustedError):
                broken()

        # At least one log record should mention SmartRetry.
        self.assertTrue(
            any("SmartRetry" in msg for msg in log_ctx.output),
            msg=f"Expected '[SmartRetry]' in logs. Got: {log_ctx.output}",
        )


class TestBaseDelayZero(unittest.TestCase):
    """base_delay=0 means retries happen without any sleeping."""

    @patch("smartretry.core.time.sleep")
    def test_sleep_called_with_zero(self, mock_sleep):
        fn = _make_flaky(fail_times=2, exc_type=Exception)
        wrapped = retry(max_retries=3, base_delay=0, backoff_factor=2.0)(fn)
        wrapped()
        for c in mock_sleep.call_args_list:
            self.assertAlmostEqual(c.args[0], 0.0)


class TestRandomFailureSimulation(unittest.TestCase):
    """
    Statistical simulation: a function that fails with a given probability
    should eventually succeed across many independent decorated calls.
    """

    def test_probabilistic_function_succeeds_within_retries(self):
        """
        A function with a 70% failure rate should succeed within 5 retries
        with probability $1 - 0.7^5 \\approx 83.2\\%$.  Over 200 independent
        calls with max_retries=5 we expect nearly all to succeed.
        """
        import random
        random.seed(42)

        failure_rate = 0.70
        successes = 0
        total_runs = 200

        @retry(max_retries=5, base_delay=0, exceptions=(RuntimeError,))
        def flaky_service():
            if random.random() < failure_rate:
                raise RuntimeError("Random failure")
            return "data"

        for _ in range(total_runs):
            try:
                result = flaky_service()
                if result == "data":
                    successes += 1
            except RetryExhaustedError:
                pass   # The rare case where all 6 attempts fail.

        # $P(\text{success per run}) = 1 - 0.7^6 \approx 88.2\%$
        # Expect > 85% success rate.
        success_rate = successes / total_runs
        self.assertGreater(
            success_rate, 0.85,
            msg=f"Success rate {success_rate:.1%} is unexpectedly low.",
        )

    def test_always_fails_never_succeeds(self):
        import random
        random.seed(0)

        @retry(max_retries=3, base_delay=0, exceptions=(ValueError,))
        def always_broken():
            raise ValueError("permanent")

        failures = 0
        for _ in range(20):
            try:
                always_broken()
            except RetryExhaustedError:
                failures += 1

        self.assertEqual(failures, 20)


# ===========================================================================
# Entry Point
# ===========================================================================

if __name__ == "__main__":
    # Pretty-print a summary header.
    print("=" * 65)
    print("  SmartRetry — Comprehensive Test Suite")
    print("=" * 65)
    unittest.main(verbosity=2)
