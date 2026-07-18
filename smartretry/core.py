# smartretry/core.py
"""
Core module for the SmartRetry library.
"""

import time
import random
import logging
import inspect
import asyncio
import threading
import functools
from typing import Callable, Optional, Tuple, Type, Any, Union

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
_logger = logging.getLogger(__name__)


# ===========================================================================
# Helper for Attribute Propagation in Decorator Chaining
# ===========================================================================

def _preserve_custom_attributes(wrapper: Any, target: Any) -> None:
    """
    Ensure custom attributes of SmartRetry (like 'stats' or 'retry_config')
    propagate upward when multiple decorators are chained in any order.
    """
    for attr in ("retry_config", "stats"):
        if hasattr(target, attr) and not hasattr(wrapper, attr):
            try:
                setattr(wrapper, attr, getattr(target, attr))
            except AttributeError:
                pass


# ===========================================================================
# Custom Exceptions
# ===========================================================================

class RetryExhaustedError(Exception):
    """Raised when all retry attempts fail and no fallback is provided."""
    def __init__(self, func_name: str, attempts: int, last_error: Exception) -> None:
        self.func_name = func_name
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"[SmartRetry] '{func_name}' failed after {attempts} attempt(s). "
            f"Last error: {type(last_error).__name__}: {last_error}"
        )


class ResultRetryTriggered(Exception):
    """Internal exception raised when a returned value triggers a retry."""
    def __init__(self, result: Any) -> None:
        self.result = result
        super().__init__(f"Result retry condition triggered for value: {result!r}")


class CircuitOpenError(Exception):
    """Raised when a execution is denied because the circuit breaker is OPEN."""
    def __init__(self, func_name: str, recovery_remaining: float) -> None:
        self.func_name = func_name
        self.recovery_remaining = recovery_remaining
        super().__init__(
            f"[SmartRetry] Circuit for '{func_name}' is OPEN. "
            f"Execution denied. Remaining cooling time: {recovery_remaining:.2f}s"
        )


class RateLimitExceededError(Exception):
    """Raised when the rate limit for a function is exceeded."""
    def __init__(self, func_name: str, retry_after: float) -> None:
        self.func_name = func_name
        self.retry_after = retry_after
        super().__init__(
            f"[SmartRetry] Rate limit exceeded for '{func_name}'. "
            f"Please wait {retry_after:.2f}s before trying again."
        )


class BulkheadFullError(Exception):
    """Raised when the bulkhead is full and execution cannot be acquired."""
    def __init__(self, func_name: str) -> None:
        self.func_name = func_name
        super().__init__(
            f"[SmartRetry] Bulkhead is full for '{func_name}'. "
            f"Execution denied due to concurrency limit."
        )


# ===========================================================================
# Runtime Context Injection, Observability Tracker & Cache Containers
# ===========================================================================

class RetryContext:
    __slots__ = ("attempt", "elapsed_time", "last_error")

    def __init__(self, attempt: int, elapsed_time: float, last_error: Optional[Exception]) -> None:
        self.attempt = attempt
        self.elapsed_time = elapsed_time
        self.last_error = last_error

    def __repr__(self) -> str:
        return (
            f"RetryContext(attempt={self.attempt}, "
            f"elapsed_time={self.elapsed_time:.3f}s, "
            f"last_error={self.last_error!r})"
        )


class RetryStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_calls = 0
        self._total_retries = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_latency = 0.0

    def increment_call(self) -> None:
        with self._lock:
            self._total_calls += 1

    def increment_retry(self) -> None:
        with self._lock:
            self._total_retries += 1

    def increment_failure(self) -> None:
        with self._lock:
            self._total_failures += 1

    def increment_success(self) -> None:
        with self._lock:
            self._total_successes += 1

    def add_latency(self, seconds: float) -> None:
        with self._lock:
            self._total_latency += seconds

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def total_retries(self) -> int:
        return self._total_retries

    @property
    def total_failures(self) -> int:
        return self._total_failures

    @property
    def total_successes(self) -> int:
        return self._total_successes

    @property
    def total_latency(self) -> float:
        return self._total_latency

    @property
    def average_latency(self) -> float:
        with self._lock:
            if self._total_calls == 0:
                return 0.0
            return self._total_latency / self._total_calls

    def __repr__(self) -> str:
        return (
            f"RetryStats(total_calls={self.total_calls}, "
            f"total_retries={self.total_retries}, "
            f"total_failures={self.total_failures}, "
            f"total_successes={self.total_successes}, "
            f"average_latency={self.average_latency:.4f}s)"
        )


class CacheEntry:
    __slots__ = ("value", "timestamp", "ttl")

    def __init__(self, value: Any, timestamp: float, ttl: Optional[float]) -> None:
        self.value = value
        self.timestamp = timestamp
        self.ttl = ttl

    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        return time.monotonic() - self.timestamp > self.ttl


# ===========================================================================
# State Machines and Thread-Safe Controllers
# ===========================================================================

class CircuitState:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int,
        recovery_timeout: float,
        exceptions: Tuple[Type[Exception], ...],
        on_state_change: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.lock = threading.Lock()
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.exceptions = exceptions
        self.on_state_change = on_state_change

        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.opened_at: Optional[float] = None

    def _transition_to(self, new_state: str) -> None:
        old_state = self.state
        if old_state != new_state:
            self.state = new_state
            if self.on_state_change is not None:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    _logger.error("[SmartRetry] Error in on_state_change callback: %s", e, exc_info=True)

    def record_success(self) -> None:
        with self.lock:
            self.consecutive_failures = 0
            self._transition_to(CircuitState.CLOSED)
            self.opened_at = None

    def record_failure(self) -> None:
        with self.lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self.opened_at = time.monotonic()
                self._transition_to(CircuitState.OPEN)

    def check_and_get_state(self) -> str:
        with self.lock:
            if self.state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self.opened_at or 0.0)
                if elapsed >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    return CircuitState.HALF_OPEN
            return self.state

    def get_remaining_recovery(self) -> float:
        with self.lock:
            if self.state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self.opened_at or 0.0)
                return max(0.0, self.recovery_timeout - elapsed)
            return 0.0


class TokenBucket:
    def __init__(self, max_requests: int, period: float) -> None:
        self.max_requests = max_requests
        self.period = period
        self.capacity = float(max_requests)
        self.tokens = self.capacity
        self.refill_rate = self.capacity / period
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def consume(self) -> Tuple[bool, float]:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.last_refill = now

            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0

            needed = 1.0 - self.tokens
            wait_time = needed / self.refill_rate
            return False, wait_time


class BulkheadLimiter:
    def __init__(self, max_concurrent_calls: int, max_wait_duration: float = 0.0) -> None:
        self.max_concurrent_calls = max_concurrent_calls
        self.max_wait_duration = max_wait_duration
        self.sync_semaphore = threading.Semaphore(max_concurrent_calls)
        self.async_semaphore: Optional[asyncio.Semaphore] = None
        self.async_lock = threading.Lock()

    def get_async_semaphore(self) -> asyncio.Semaphore:
        with self.async_lock:
            if self.async_semaphore is None:
                self.async_semaphore = asyncio.Semaphore(self.max_concurrent_calls)
            return self.async_semaphore


# ===========================================================================
# Configuration Container & Key Builders
# ===========================================================================

class RetryConfig:
    __slots__ = (
        "max_retries",
        "base_delay",
        "backoff_factor",
        "exceptions",
        "fallback",
        "logger",
        "jitter",
        "on_retry",
        "retry_on_result",
        "total_timeout",
        "dynamic_delay",
    )

    def __init__(
        self,
        max_retries: int,
        base_delay: float,
        backoff_factor: float,
        exceptions: Tuple[Type[Exception], ...],
        fallback: Optional[Callable],
        logger: Optional[logging.Logger],
        jitter: Union[bool, Callable[[float], float]] = False,
        on_retry: Optional[Callable[[Exception, int, float], None]] = None,
        retry_on_result: Optional[Callable[[Any], bool]] = None,
        total_timeout: Optional[float] = None,
        dynamic_delay: Optional[Callable[[Union[Exception, Any]], Optional[float]]] = None,
    ) -> None:
        # --- Type validation -----------------------------------------------
        if not isinstance(max_retries, int):
            raise TypeError(f"max_retries must be int, got {type(max_retries).__name__!r}")
        if not isinstance(base_delay, (int, float)):
            raise TypeError(f"base_delay must be numeric, got {type(base_delay).__name__!r}")
        if not isinstance(backoff_factor, (int, float)):
            raise TypeError(
                f"backoff_factor must be numeric, got {type(backoff_factor).__name__!r}"
            )
        if not isinstance(exceptions, tuple) or not exceptions:
            raise TypeError("exceptions must be a non-empty tuple of Exception subclasses.")
        for exc_type in exceptions:
            if not (isinstance(exc_type, type) and issubclass(exc_type, Exception)):
                raise TypeError(
                    f"Every item in exceptions must be an Exception subclass; got {exc_type!r}"
                )
        if fallback is not None and not callable(fallback):
            raise TypeError(f"fallback must be callable or None, got {type(fallback).__name__!r}")
        if logger is not None and not isinstance(logger, logging.Logger):
            raise TypeError(
                f"logger must be a logging.Logger instance or None, got {type(logger).__name__!r}"
            )
        if not isinstance(jitter, bool) and not callable(jitter):
            raise TypeError(f"jitter must be a boolean or a callable, got {type(jitter).__name__!r}")
        if on_retry is not None and not callable(on_retry):
            raise TypeError(f"on_retry must be a callable or None, got {type(on_retry).__name__!r}")
        if retry_on_result is not None and not callable(retry_on_result):
            raise TypeError(f"retry_on_result must be a callable, got {type(retry_on_result).__name__!r}")
        if total_timeout is not None:
            if not isinstance(total_timeout, (int, float)):
                raise TypeError(f"total_timeout must be numeric, got {type(total_timeout).__name__!r}")
            if total_timeout <= 0:
                raise ValueError(f"total_timeout must be > 0, got {total_timeout}")
        if dynamic_delay is not None and not callable(dynamic_delay):
            raise TypeError(f"dynamic_delay must be a callable, got {type(dynamic_delay).__name__!r}")

        # --- Value validation -----------------------------------------------
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        if base_delay < 0:
            raise ValueError(f"base_delay must be >= 0, got {base_delay}")
        if backoff_factor < 1.0:
            raise ValueError(f"backoff_factor must be >= 1.0, got {backoff_factor}")

        # --- Assignment -----------------------------------------------------
        object.__setattr__(self, "max_retries", max_retries)
        object.__setattr__(self, "base_delay", float(base_delay))
        object.__setattr__(self, "backoff_factor", float(backoff_factor))
        object.__setattr__(self, "exceptions", exceptions)
        object.__setattr__(self, "fallback", fallback)
        object.__setattr__(self, "logger", logger or _logger)
        object.__setattr__(self, "jitter", jitter)
        object.__setattr__(self, "on_retry", on_retry)
        object.__setattr__(self, "retry_on_result", retry_on_result)
        object.__setattr__(self, "total_timeout", float(total_timeout) if total_timeout is not None else None)
        object.__setattr__(self, "dynamic_delay", dynamic_delay)

    def __setattr__(self, key: str, value: Any) -> None:  # pragma: no cover
        raise AttributeError("RetryConfig is immutable.")

    def compute_delay(self, attempt: int) -> float:
        raw_delay = self.base_delay * (self.backoff_factor ** attempt)
        if self.jitter is True:
            return random.uniform(0.0, raw_delay)
        elif callable(self.jitter):
            return self.jitter(raw_delay)
        return raw_delay

    def resolve_dynamic_delay(self, last_exception: Optional[Exception]) -> Optional[float]:
        if self.dynamic_delay is None or last_exception is None:
            return None
        try:
            arg = last_exception
            if isinstance(last_exception, ResultRetryTriggered):
                arg = last_exception.result
            val = self.dynamic_delay(arg)
            if val is not None:
                return float(val)
        except Exception as e:
            self.logger.error("[SmartRetry] Error resolving dynamic_delay: %s", e, exc_info=True)
        return None

    def __repr__(self) -> str:
        exc_names = ", ".join(e.__name__ for e in self.exceptions)
        return (
            f"RetryConfig("
            f"max_retries={self.max_retries}, "
            f"base_delay={self.base_delay}, "
            f"backoff_factor={self.backoff_factor}, "
            f"exceptions=({exc_names}), "
            f"fallback={self.fallback!r}, "
            f"jitter={self.jitter!r}, "
            f"total_timeout={self.total_timeout}"
            f")"
        )


def _make_key(args: tuple, kwargs: dict) -> Any:
    """Safely build hashable cache keys to avoid unhashable type errors."""
    try:
        return (args, frozenset(kwargs.items()))
    except TypeError:
        return (str(args), str(sorted(kwargs.items())))


# ===========================================================================
# Internal Execution Engines
# ===========================================================================

def _execute_with_retry(
    func: Callable,
    config: RetryConfig,
    args: Tuple[Any, ...],
    kwargs: dict,
    stats: RetryStats,
) -> Any:
    log = config.logger
    func_name = getattr(func, "__qualname__", repr(func))
    total_attempts = config.max_retries + 1
    last_exception: Optional[Exception] = None
    start_time = time.monotonic()

    has_context_param = False
    try:
        sig = inspect.signature(func)
        if "retry_context" in sig.parameters:
            param = sig.parameters["retry_context"]
            if param.kind != inspect.Parameter.POSITIONAL_ONLY:
                has_context_param = True
    except ValueError:
        pass

    stats.increment_call()

    for attempt in range(total_attempts):
        is_retry = attempt > 0

        if is_retry:
            delay = config.compute_delay(attempt - 1)
            resolved_delay = config.resolve_dynamic_delay(last_exception)
            if resolved_delay is not None:
                delay = resolved_delay

            # --- Timeout budget analysis ---
            elapsed = time.monotonic() - start_time
            if config.total_timeout is not None:
                if elapsed >= config.total_timeout or elapsed + delay > config.total_timeout:
                    log.error(
                        "[SmartRetry] Sleep delay %.3fs would exceed total timeout budget of %.2fs. Aborting.",
                        delay,
                        config.total_timeout
                    )
                    stats.increment_failure()
                    if config.fallback is not None:
                        log.warning("[SmartRetry] Invoking fallback for '%s' due to timeout.", func_name)
                        return config.fallback(*args, **kwargs)
                    raise RetryExhaustedError(
                        func_name=func_name,
                        attempts=attempt,
                        last_error=last_exception or TimeoutError(f"Global timeout budget of {config.total_timeout}s exhausted.")
                    )

            stats.increment_retry()
            log.warning(
                "[SmartRetry] Retry %d/%d for '%s' — waiting %.3fs (last error: %s)",
                attempt,
                config.max_retries,
                func_name,
                delay,
                f"{type(last_exception).__name__}: {last_exception}" if last_exception else "None",
            )
            if config.on_retry is not None:
                try:
                    config.on_retry(last_exception, attempt, delay)  # type: ignore
                except Exception as callback_exc:
                    log.error("[SmartRetry] Error executing on_retry callback: %s", callback_exc, exc_info=True)
            time.sleep(delay)

        if has_context_param:
            kwargs["retry_context"] = RetryContext(
                attempt=attempt + 1,
                elapsed_time=time.monotonic() - start_time,
                last_error=last_exception
            )

        fn_start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            latency = time.monotonic() - fn_start
            stats.add_latency(latency)

            # Evaluate return value predicate
            if config.retry_on_result is not None and config.retry_on_result(result):
                raise ResultRetryTriggered(result)

            if is_retry:
                log.info(
                    "[SmartRetry] '%s' succeeded on attempt %d/%d.",
                    func_name,
                    attempt + 1,
                    total_attempts,
                )
            stats.increment_success()
            return result

        except (config.exceptions + (ResultRetryTriggered,)) as exc:
            latency = time.monotonic() - fn_start
            stats.add_latency(latency)
            last_exception = exc

        except Exception:
            latency = time.monotonic() - fn_start
            stats.add_latency(latency)
            log.error(
                "[SmartRetry] Non-retryable exception in '%s'. Re-raising immediately.",
                func_name,
                exc_info=True,
            )
            stats.increment_failure()
            raise

    log.error(
        "[SmartRetry] All %d attempt(s) for '%s' failed. Last error: %s",
        total_attempts,
        func_name,
        f"{type(last_exception).__name__}: {last_exception}" if last_exception else "None",
    )

    stats.increment_failure()
    if config.fallback is not None:
        log.warning("[SmartRetry] Invoking fallback for '%s'.", func_name)
        return config.fallback(*args, **kwargs)

    raise RetryExhaustedError(
        func_name=func_name,
        attempts=total_attempts,
        last_error=last_exception,  # type: ignore[arg-type]
    )


async def _execute_with_retry_async(
    func: Callable,
    config: RetryConfig,
    args: Tuple[Any, ...],
    kwargs: dict,
    stats: RetryStats,
) -> Any:
    log = config.logger
    func_name = getattr(func, "__qualname__", repr(func))
    total_attempts = config.max_retries + 1
    last_exception: Optional[Exception] = None
    start_time = time.monotonic()

    has_context_param = False
    try:
        sig = inspect.signature(func)
        if "retry_context" in sig.parameters:
            param = sig.parameters["retry_context"]
            if param.kind != inspect.Parameter.POSITIONAL_ONLY:
                has_context_param = True
    except ValueError:
        pass

    stats.increment_call()

    for attempt in range(total_attempts):
        is_retry = attempt > 0

        if is_retry:
            delay = config.compute_delay(attempt - 1)
            resolved_delay = config.resolve_dynamic_delay(last_exception)
            if resolved_delay is not None:
                delay = resolved_delay

            # --- Timeout budget analysis ---
            elapsed = time.monotonic() - start_time
            if config.total_timeout is not None:
                if elapsed >= config.total_timeout or elapsed + delay > config.total_timeout:
                    log.error(
                        "[SmartRetry] Sleep delay %.3fs would exceed total timeout budget of %.2fs. Aborting.",
                        delay,
                        config.total_timeout
                    )
                    stats.increment_failure()
                    if config.fallback is not None:
                        log.warning("[SmartRetry] Invoking fallback for '%s' due to timeout.", func_name)
                        if inspect.iscoroutinefunction(config.fallback):
                            return await config.fallback(*args, **kwargs)
                        return config.fallback(*args, **kwargs)
                    raise RetryExhaustedError(
                        func_name=func_name,
                        attempts=attempt,
                        last_error=last_exception or TimeoutError(f"Global timeout budget of {config.total_timeout}s exhausted.")
                    )

            stats.increment_retry()
            log.warning(
                "[SmartRetry] Retry %d/%d for '%s' — waiting %.3fs (last error: %s)",
                attempt,
                config.max_retries,
                func_name,
                delay,
                f"{type(last_exception).__name__}: {last_exception}" if last_exception else "None",
            )
            if config.on_retry is not None:
                try:
                    if inspect.iscoroutinefunction(config.on_retry):
                        await config.on_retry(last_exception, attempt, delay)  # type: ignore
                    else:
                        config.on_retry(last_exception, attempt, delay)  # type: ignore
                except Exception as callback_exc:
                    log.error("[SmartRetry] Error executing on_retry callback: %s", callback_exc, exc_info=True)
            await asyncio.sleep(delay)

        if has_context_param:
            kwargs["retry_context"] = RetryContext(
                attempt=attempt + 1,
                elapsed_time=time.monotonic() - start_time,
                last_error=last_exception
            )

        fn_start = time.monotonic()
        try:
            result = await func(*args, **kwargs)
            latency = time.monotonic() - fn_start
            stats.add_latency(latency)

            # Evaluate return value predicate
            if config.retry_on_result is not None and config.retry_on_result(result):
                raise ResultRetryTriggered(result)

            if is_retry:
                log.info(
                    "[SmartRetry] '%s' succeeded on attempt %d/%d.",
                    func_name,
                    attempt + 1,
                    total_attempts,
                )
            stats.increment_success()
            return result

        except (config.exceptions + (ResultRetryTriggered,)) as exc:
            latency = time.monotonic() - fn_start
            stats.add_latency(latency)
            last_exception = exc

        except Exception:
            latency = time.monotonic() - fn_start
            stats.add_latency(latency)
            log.error(
                "[SmartRetry] Non-retryable exception in '%s'. Re-raising immediately.",
                func_name,
                exc_info=True,
            )
            stats.increment_failure()
            raise

    log.error(
        "[SmartRetry] All %d attempt(s) for '%s' failed. Last error: %s",
        total_attempts,
        func_name,
        f"{type(last_exception).__name__}: {last_exception}" if last_exception else "None",
    )

    stats.increment_failure()
    if config.fallback is not None:
        log.warning("[SmartRetry] Invoking fallback for '%s'.", func_name)
        if inspect.iscoroutinefunction(config.fallback):
            return await config.fallback(*args, **kwargs)
        return config.fallback(*args, **kwargs)

    raise RetryExhaustedError(
        func_name=func_name,
        attempts=total_attempts,
        last_error=last_exception,  # type: ignore[arg-type]
    )


# ===========================================================================
# Public Decorators
# ===========================================================================

def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    fallback: Optional[Callable] = None,
    logger: Optional[logging.Logger] = None,
    jitter: Union[bool, Callable[[float], float]] = False,
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
    retry_on_result: Optional[Callable[[Any], bool]] = None,
    total_timeout: Optional[float] = None,
    dynamic_delay: Optional[Callable[[Union[Exception, Any]], Optional[float]]] = None,
) -> Callable:
    """
    Decorator factory that wraps a function with exponential-backoff retry logic.
    """
    config = RetryConfig(
        max_retries=max_retries,
        base_delay=base_delay,
        backoff_factor=backoff_factor,
        exceptions=exceptions,
        fallback=fallback,
        logger=logger,
        jitter=jitter,
        on_retry=on_retry,
        retry_on_result=retry_on_result,
        total_timeout=total_timeout,
        dynamic_delay=dynamic_delay,
    )

    def decorator(func: Callable) -> Callable:
        stats = RetryStats()

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _execute_with_retry_async(func, config, args, kwargs, stats)
            async_wrapper.retry_config = config  # type: ignore[attr-defined]
            async_wrapper.stats = stats          # type: ignore[attr-defined]
            _preserve_custom_attributes(async_wrapper, func)
            return async_wrapper
        else:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return _execute_with_retry(func, config, args, kwargs, stats)
            wrapper.retry_config = config  # type: ignore[attr-defined]
            wrapper.stats = stats          # type: ignore[attr-defined]
            _preserve_custom_attributes(wrapper, func)
            return wrapper

    return decorator


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_state_change: Optional[Callable[[str, str], None]] = None,
) -> Callable:
    """
    A circuit breaker decorator protecting downstream resources from being overwhelmed.
    """
    if not isinstance(failure_threshold, int) or failure_threshold <= 0:
        raise ValueError("failure_threshold must be an integer > 0")
    if not isinstance(recovery_timeout, (int, float)) or recovery_timeout <= 0:
        raise ValueError("recovery_timeout must be a float/int > 0")
    if not isinstance(exceptions, tuple) or not exceptions:
        raise TypeError("exceptions must be a non-empty tuple of Exception subclasses.")
    for exc_type in exceptions:
        if not (isinstance(exc_type, type) and issubclass(exc_type, Exception)):
            raise TypeError(f"Every item in exceptions must be an Exception subclass; got {exc_type!r}")

    state = CircuitState(failure_threshold, recovery_timeout, exceptions, on_state_change)

    def decorator(func: Callable) -> Callable:
        func_name = getattr(func, "__qualname__", repr(func))

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                current_state = state.check_and_get_state()
                if current_state == CircuitState.OPEN:
                    raise CircuitOpenError(func_name, state.get_remaining_recovery())

                try:
                    result = await func(*args, **kwargs)
                    state.record_success()
                    return result
                except exceptions:
                    state.record_failure()
                    raise
            _preserve_custom_attributes(async_wrapper, func)
            return async_wrapper
        else:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                current_state = state.check_and_get_state()
                if current_state == CircuitState.OPEN:
                    raise CircuitOpenError(func_name, state.get_remaining_recovery())

                try:
                    result = func(*args, **kwargs)
                    state.record_success()
                    return result
                except exceptions:
                    state.record_failure()
                    raise
            _preserve_custom_attributes(wrapper, func)
            return wrapper

    return decorator


def rate_limiter(max_requests: int, period: float = 1.0) -> Callable:
    """
    Token-bucket rate limiter. Sync & Async safe.
    """
    if max_requests <= 0:
        raise ValueError("max_requests must be > 0")
    if period <= 0:
        raise ValueError("period must be > 0")

    bucket = TokenBucket(max_requests, period)

    def decorator(func: Callable) -> Callable:
        func_name = getattr(func, "__qualname__", repr(func))

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                allowed, wait_time = bucket.consume()
                if not allowed:
                    raise RateLimitExceededError(func_name, wait_time)
                return await func(*args, **kwargs)
            _preserve_custom_attributes(async_wrapper, func)
            return async_wrapper
        else:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                allowed, wait_time = bucket.consume()
                if not allowed:
                    raise RateLimitExceededError(func_name, wait_time)
                return func(*args, **kwargs)
            _preserve_custom_attributes(wrapper, func)
            return wrapper

    return decorator


def bulkhead(max_concurrent_calls: int, max_wait_duration: float = 0.0) -> Callable:
    """
    Bulkhead Concurrency Limiter. Sync & Async safe.
    """
    if not isinstance(max_concurrent_calls, int) or max_concurrent_calls <= 0:
        raise ValueError("max_concurrent_calls must be an integer > 0")
    if not isinstance(max_wait_duration, (int, float)) or max_wait_duration < 0:
        raise ValueError("max_wait_duration must be a numeric value >= 0")

    limiter = BulkheadLimiter(max_concurrent_calls, max_wait_duration)

    def decorator(func: Callable) -> Callable:
        func_name = getattr(func, "__qualname__", repr(func))

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                sem = limiter.get_async_semaphore()
                if max_wait_duration == 0.0:
                    if sem.locked():
                        raise BulkheadFullError(func_name)
                    await sem.acquire()
                else:
                    try:
                        await asyncio.wait_for(sem.acquire(), timeout=max_wait_duration)
                    except asyncio.TimeoutError:
                        raise BulkheadFullError(func_name)

                try:
                    return await func(*args, **kwargs)
                finally:
                    sem.release()
            _preserve_custom_attributes(async_wrapper, func)
            return async_wrapper
        else:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if max_wait_duration == 0.0:
                    acquired = limiter.sync_semaphore.acquire(blocking=False)
                else:
                    acquired = limiter.sync_semaphore.acquire(timeout=max_wait_duration)

                if not acquired:
                    raise BulkheadFullError(func_name)

                try:
                    return func(*args, **kwargs)
                finally:
                    limiter.sync_semaphore.release()
            _preserve_custom_attributes(wrapper, func)
            return wrapper

    return decorator


def fallback(
    fallback_value_or_callable: Any,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    An independent fallback policy decorator.
    """
    if not isinstance(exceptions, tuple) or not exceptions:
        raise TypeError("exceptions must be a non-empty tuple of Exception subclasses.")

    def decorator(func: Callable) -> Callable:
        func_name = getattr(func, "__qualname__", repr(func))

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    _logger.warning("[SmartRetry] Standalone Fallback triggered for '%s' due to: %s", func_name, exc)
                    if callable(fallback_value_or_callable):
                        if inspect.iscoroutinefunction(fallback_value_or_callable):
                            return await fallback_value_or_callable(*args, **kwargs)
                        return fallback_value_or_callable(*args, **kwargs)
                    return fallback_value_or_callable
            _preserve_custom_attributes(async_wrapper, func)
            return async_wrapper
        else:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    _logger.warning("[SmartRetry] Standalone Fallback triggered for '%s' due to: %s", func_name, exc)
                    if callable(fallback_value_or_callable):
                        return fallback_value_or_callable(*args, **kwargs)
                    return fallback_value_or_callable
            _preserve_custom_attributes(wrapper, func)
            return wrapper

    return decorator


def resilient_cache(
    ttl: Optional[float] = None,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    Provides graceful stale degradation (Stale-While-Revalidate).
    """
    if ttl is not None and (not isinstance(ttl, (int, float)) or ttl <= 0):
        raise ValueError("ttl must be a positive numeric value or None")
    if not isinstance(exceptions, tuple) or not exceptions:
        raise TypeError("exceptions must be a non-empty tuple of Exception subclasses.")

    store: dict = {}
    lock = threading.Lock()

    def decorator(func: Callable) -> Callable:
        func_name = getattr(func, "__qualname__", repr(func))

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                key = _make_key(args, kwargs)
                try:
                    result = await func(*args, **kwargs)
                    with lock:
                        store[key] = CacheEntry(result, time.monotonic(), ttl)
                    return result
                except exceptions as exc:
                    with lock:
                        entry = store.get(key)
                    if entry is not None and not entry.is_expired():
                        _logger.warning(
                            "[SmartRetry] Resilient Cache hit for '%s' after suppressing exception: %s: %s",
                            func_name, type(exc).__name__, exc
                        )
                        return entry.value
                    raise
            _preserve_custom_attributes(async_wrapper, func)
            return async_wrapper
        else:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                key = _make_key(args, kwargs)
                try:
                    result = func(*args, **kwargs)
                    with lock:
                        store[key] = CacheEntry(result, time.monotonic(), ttl)
                    return result
                except exceptions as exc:
                    with lock:
                        entry = store.get(key)
                    if entry is not None and not entry.is_expired():
                        _logger.warning(
                            "[SmartRetry] Resilient Cache hit for '%s' after suppressing exception: %s: %s",
                            func_name, type(exc).__name__, exc
                        )
                        return entry.value
                    raise
            _preserve_custom_attributes(wrapper, func)
            return wrapper

    return decorator