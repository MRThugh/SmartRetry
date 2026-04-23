# smartretry/__init__.py
"""
SmartRetry
==========
A pure Python library providing a highly advanced, customizable
retry decorator to solve the "flaky function" problem.

Public API:
    - retry: The main decorator factory.
    - RetryExhaustedError: Raised when all retry attempts are exhausted
                           and no fallback is provided.

Usage Example:
    >>> from smartretry import retry
    >>>
    >>> @retry(max_retries=3, base_delay=0.5, backoff_factor=2.0)
    ... def unstable_api_call():
    ...     # ... your flaky logic here
    ...     pass
"""

from smartretry.core import retry, RetryExhaustedError

__all__ = ["retry", "RetryExhaustedError"]
__version__ = "1.0.0"
__author__ = "Ali Kamrani"
