# smartretry/__init__.py
"""
SmartRetry
==========
A pure Python library providing custom resilience tools like retry,
circuit breaker, bulkhead, rate limiter, fallback, resilient cache, 
and advanced execution observability.

Public API:
    - retry: The main retry decorator factory.
    - circuit_breaker: The circuit breaker decorator.
    - rate_limiter: Token-bucket rate limiter decorator.
    - bulkhead: Semaphore-based bulkhead concurrency limiter decorator.
    - fallback: Independent policy fallback decorator.
    - resilient_cache: Graceful stale degradation cache decorator.
    - RetryExhaustedError: Raised when all retry attempts are exhausted.
    - CircuitOpenError: Raised when execution is denied by an open circuit.
    - RateLimitExceededError: Raised when rate limit constraints are violated.
    - BulkheadFullError: Raised when concurrency bulkhead is saturated.
    - RetryContext: Information passed directly into the decorated function.
"""

from smartretry.core import (
    retry,
    circuit_breaker,
    rate_limiter,
    bulkhead,
    fallback,
    resilient_cache,
    RetryExhaustedError,
    CircuitOpenError,
    RateLimitExceededError,
    BulkheadFullError,
    RetryContext,
)

__all__ = [
    "retry",
    "circuit_breaker",
    "rate_limiter",
    "bulkhead",
    "fallback",
    "resilient_cache",
    "RetryExhaustedError",
    "CircuitOpenError",
    "RateLimitExceededError",
    "BulkheadFullError",
    "RetryContext",
]
__version__ = "1.0.0"
__author__ = "Ali Kamrani"