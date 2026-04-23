# smartretry/core.py
"""
Core module for the SmartRetry library.

This module contains:
    - RetryExhaustedError: Custom exception for exhausted retries.
    - RetryConfig: A validated, immutable configuration dataclass.
    - _execute_with_retry: The internal engine that drives retry logic.
    - retry: The public-facing decorator factory.

Design Principles:
    - Separation of Concerns: Configuration, execution, and decoration
      are handled in distinct, testable units.
    - Fail-Fast: Exceptions not listed in `exceptions` are re-raised
      immediately without consuming retry budget.
    - Observability: Every significant event is logged via the standard
      `logging` module, requiring zero external dependencies.
"""

import time
import logging
import functools
from typing import Callable, Optional, Tuple, Type, Any, Union

# ---------------------------------------------------------------------------
# Module-level logger — consumers can configure it via logging.getLogger()
# ---------------------------------------------------------------------------
_logger = logging.getLogger(__name__)


# ===========================================================================
# Custom Exception
# ===========================================================================

class RetryExhaustedError(Exception):
    """
    Raised when a decorated function fails on every attempt and
    no fallback callable has been provided.

    Attributes:
        attempts    (int): Total number of attempts that were made.
        last_error  (Exception): The final exception that caused failure.
        func_name   (str): Qualified name of the function that failed.

    Example:
        >>> try:
        ...     flaky()
        ... except RetryExhaustedError as exc:
        ...     print(exc.attempts, exc.last_error)
    """

    def __init__(
        self,
        func_name: str,
        attempts: int,
        last_error: Exception,
    ) -> None:
        self.func_name = func_name
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"[SmartRetry] '{func_name}' failed after {attempts} attempt(s). "
            f"Last error: {type(last_error).__name__}: {last_error}"
        )


# ===========================================================================
# Configuration Container
# ===========================================================================

class RetryConfig:
    """
    Immutable, validated configuration object for the retry decorator.

    All validation is performed at construction time so that
    misconfiguration is caught immediately (at decoration time),
    not at the first call.

    Args:
        max_retries     (int): Maximum extra attempts after the first failure.
                               Must be >= 0.
        base_delay      (float): Seconds to wait before the first retry.
                                 Must be >= 0.
        backoff_factor  (float): Exponential multiplier applied each round.
                                 Must be >= 1.0.
                                 Delay formula: base_delay × backoff_factor^attempt
        exceptions      (Tuple[Type[Exception], ...]): Exception types that
                                 should trigger a retry. Any other exception
                                 is re-raised immediately.
        fallback        (Optional[Callable]): Called with the same positional
                                 and keyword arguments as the original function
                                 when all retries are exhausted.  Its return
                                 value becomes the final result.
        logger          (Optional[logging.Logger]): Custom logger; falls back
                                 to the module-level logger if None.

    Raises:
        TypeError:  If any argument has an incorrect type.
        ValueError: If any numeric argument is out of range.
    """

    __slots__ = (
        "max_retries",
        "base_delay",
        "backoff_factor",
        "exceptions",
        "fallback",
        "logger",
    )

    def __init__(
        self,
        max_retries: int,
        base_delay: float,
        backoff_factor: float,
        exceptions: Tuple[Type[Exception], ...],
        fallback: Optional[Callable],
        logger: Optional[logging.Logger],
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
                    f"Every item in exceptions must be an Exception subclass; "
                    f"got {exc_type!r}"
                )
        if fallback is not None and not callable(fallback):
            raise TypeError(f"fallback must be callable or None, got {type(fallback).__name__!r}")
        if logger is not None and not isinstance(logger, logging.Logger):
            raise TypeError(
                f"logger must be a logging.Logger instance or None, "
                f"got {type(logger).__name__!r}"
            )

        # --- Value validation -----------------------------------------------
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        if base_delay < 0:
            raise ValueError(f"base_delay must be >= 0, got {base_delay}")
        if backoff_factor < 1.0:
            raise ValueError(f"backoff_factor must be >= 1.0, got {backoff_factor}")

        # --- Assignment (immutable after __init__) --------------------------
        object.__setattr__(self, "max_retries", max_retries)
        object.__setattr__(self, "base_delay", float(base_delay))
        object.__setattr__(self, "backoff_factor", float(backoff_factor))
        object.__setattr__(self, "exceptions", exceptions)
        object.__setattr__(self, "fallback", fallback)
        object.__setattr__(self, "logger", logger or _logger)

    def __setattr__(self, key: str, value: Any) -> None:  # pragma: no cover
        raise AttributeError("RetryConfig is immutable.")

    def compute_delay(self, attempt: int) -> float:
        """
        Calculate the wait time before a specific retry attempt.

        Formula:
            $delay = base\_delay \\times backoff\_factor^{attempt}$

        Args:
            attempt (int): Zero-based attempt index (0 = first retry).

        Returns:
            float: Seconds to sleep before this attempt.

        Example:
            >>> cfg = RetryConfig(3, 1.0, 2.0, (Exception,), None, None)
            >>> cfg.compute_delay(0)   # 1.0 × 2^0 = 1.0
            1.0
            >>> cfg.compute_delay(1)   # 1.0 × 2^1 = 2.0
            2.0
            >>> cfg.compute_delay(2)   # 1.0 × 2^2 = 4.0
            4.0
        """
        return self.base_delay * (self.backoff_factor ** attempt)

    def __repr__(self) -> str:
        exc_names = ", ".join(e.__name__ for e in self.exceptions)
        return (
            f"RetryConfig("
            f"max_retries={self.max_retries}, "
            f"base_delay={self.base_delay}, "
            f"backoff_factor={self.backoff_factor}, "
            f"exceptions=({exc_names}), "
            f"fallback={self.fallback!r}"
            f")"
        )


# ===========================================================================
# Internal Retry Engine
# ===========================================================================

def _execute_with_retry(
    func: Callable,
    config: RetryConfig,
    args: Tuple[Any, ...],
    kwargs: dict,
) -> Any:
    """
    Internal engine that executes *func* with exponential-backoff retry logic.

    This function is intentionally separated from the decorator machinery
    so it can be unit-tested in isolation.

    Execution flow:
        1. Attempt #0  — call the function directly (no delay).
        2. On a *retryable* exception, log a WARNING and sleep for
           $base\\_delay \\times backoff\\_factor^{attempt}$ seconds.
        3. Repeat up to *max_retries* additional attempts.
        4. If every attempt fails:
            a. If *fallback* is set → call fallback(*args, **kwargs)
               and return its result.
            b. Otherwise → raise RetryExhaustedError.
        5. Any exception NOT in *config.exceptions* is re-raised
           immediately without touching the retry counter.

    Args:
        func   (Callable): The original, unwrapped function.
        config (RetryConfig): Validated configuration object.
        args   (tuple): Positional arguments forwarded to *func*.
        kwargs (dict): Keyword arguments forwarded to *func*.

    Returns:
        Any: Return value of *func* (or *fallback*) on success.

    Raises:
        Exception:          Any non-retryable exception from *func*.
        RetryExhaustedError: When all attempts are exhausted and
                             *fallback* is None.
    """
    log = config.logger
    func_name = getattr(func, "__qualname__", repr(func))
    total_attempts = config.max_retries + 1   # initial call + retries
    last_exception: Optional[Exception] = None

    for attempt in range(total_attempts):
        is_retry = attempt > 0

        # -- Delay before retry (never before the very first attempt) -------
        if is_retry:
            delay = config.compute_delay(attempt - 1)
            log.warning(
                "[SmartRetry] Retry %d/%d for '%s' — waiting %.3fs "
                "(last error: %s: %s)",
                attempt,
                config.max_retries,
                func_name,
                delay,
                type(last_exception).__name__,
                last_exception,
            )
            time.sleep(delay)

        # -- Function call ---------------------------------------------------
        try:
            result = func(*args, **kwargs)
            if is_retry:
                log.info(
                    "[SmartRetry] '%s' succeeded on attempt %d/%d.",
                    func_name,
                    attempt + 1,
                    total_attempts,
                )
            return result

        except config.exceptions as exc:
            # Retryable exception — record and continue the loop.
            last_exception = exc

        except Exception:
            # Non-retryable exception — escalate immediately.
            log.error(
                "[SmartRetry] Non-retryable exception in '%s'. Re-raising immediately.",
                func_name,
                exc_info=True,
            )
            raise

    # -----------------------------------------------------------------------
    # All attempts exhausted
    # -----------------------------------------------------------------------
    log.error(
        "[SmartRetry] All %d attempt(s) for '%s' failed. Last error: %s: %s",
        total_attempts,
        func_name,
        type(last_exception).__name__,
        last_exception,
    )

    if config.fallback is not None:
        log.warning(
            "[SmartRetry] Invoking fallback for '%s'.",
            func_name,
        )
        return config.fallback(*args, **kwargs)

    raise RetryExhaustedError(
        func_name=func_name,
        attempts=total_attempts,
        last_error=last_exception,  # type: ignore[arg-type]
    )


# ===========================================================================
# Public Decorator Factory
# ===========================================================================

def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    fallback: Optional[Callable] = None,
    logger: Optional[logging.Logger] = None,
) -> Callable:
    """
    Decorator factory that wraps a function with exponential-backoff retry logic.

    The decorated function will be re-invoked automatically whenever it raises
    one of the specified *exceptions*, up to *max_retries* additional times.
    Between attempts the caller's thread sleeps for:

        $delay = base\\_delay \\times backoff\\_factor^{attempt}$

    where *attempt* is zero-based (0 = first retry, 1 = second retry, …).

    Args:
        max_retries    (int, optional): Extra attempts after the first failure.
                            Defaults to 3.  Must be >= 0.
        base_delay     (float, optional): Seconds to wait before the first
                            retry.  Defaults to 1.0.  Must be >= 0.
        backoff_factor (float, optional): Exponential multiplier applied each
                            round.  Defaults to 2.0.  Must be >= 1.0.
        exceptions     (tuple, optional): Exception types that trigger a retry.
                            Defaults to (Exception,) — catches everything.
                            Any exception NOT in this tuple is re-raised
                            immediately.
        fallback       (callable, optional): Function called with the same
                            arguments as the original when all retries are
                            exhausted.  Its return value becomes the result.
                            Defaults to None (raises RetryExhaustedError).
        logger         (logging.Logger, optional): Custom logger for retry
                            events.  Defaults to the smartretry.core logger.

    Returns:
        Callable: A decorator that applies the retry logic to any function.

    Raises:
        TypeError:  Immediately at decoration time if any argument is invalid.
        ValueError: Immediately at decoration time if any numeric arg is bad.

    Examples:
        Basic usage with default settings::

            @retry()
            def call_api():
                ...

        Retry only on network errors, three times, with 0.5 s initial delay::

            @retry(
                max_retries=3,
                base_delay=0.5,
                backoff_factor=2.0,
                exceptions=(ConnectionError, TimeoutError),
            )
            def fetch_data(url: str) -> dict:
                ...

        With a fallback function::

            def serve_cached(url: str) -> dict:
                return {"cached": True}

            @retry(max_retries=2, fallback=serve_cached)
            def fetch_data(url: str) -> dict:
                ...

        Delay schedule for base_delay=1, backoff_factor=2:

            * Retry 1: $1.0 \\times 2^{0} = 1.0$ s
            * Retry 2: $1.0 \\times 2^{1} = 2.0$ s
            * Retry 3: $1.0 \\times 2^{2} = 4.0$ s
    """
    # Build & validate config eagerly — fail loud at decoration time.
    config = RetryConfig(
        max_retries=max_retries,
        base_delay=base_delay,
        backoff_factor=backoff_factor,
        exceptions=exceptions,
        fallback=fallback,
        logger=logger,
    )

    def decorator(func: Callable) -> Callable:
        """Attach retry logic to *func* while preserving its metadata."""

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _execute_with_retry(func, config, args, kwargs)

        # Expose the config on the wrapper for introspection / testing.
        wrapper.retry_config = config  # type: ignore[attr-defined]
        return wrapper

    return decorator
