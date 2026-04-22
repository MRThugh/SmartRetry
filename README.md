# SmartRetry

A lightweight, dependency‑free retry decorator for Python with configurable retries, exponential backoff, logging support, and optional fallback handling.

**Author:** Ali Kamrani  
**GitHub:** https://github.com/MRThugh

---

# Overview

**SmartRetry** is a simple and reliable Python library that helps you automatically retry functions when they fail.

In real applications, operations like:

- API calls  
- Database queries  
- Network requests  
- File operations  

may fail temporarily. Retrying these operations often solves the problem, but implementing retry logic repeatedly across projects becomes messy.

SmartRetry solves this by providing a clean **decorator‑based retry system** with configurable behavior.

Key features:

- Simple and Pythonic **retry decorator**
- **Exponential backoff** for retry delays
- **Zero external dependencies**
- Optional **fallback function**
- Support for **specific exception types**
- Optional **logging support**
- Clean and minimal API
- Fully testable design

---

# Installation

## Install locally (development mode)

If you cloned the repository:

```
pip install -e .
```

## Install via pip (after publishing to PyPI)

```
pip install smartretry
```

---

# Quick Start

Basic example:

```python
from smartretry import retry

@retry(
    max_retries=3,
    base_delay=0.5,
    backoff_factor=2.0,
    exceptions=(ConnectionError,)
)
def fetch_data():
    print("Attempting request...")
    raise ConnectionError("Network unstable")

fetch_data()
```

Example output:

```
Attempting request...
Retry 1/3 after 0.5 seconds
Attempting request...
Retry 2/3 after 1.0 seconds
Attempting request...
Retry 3/3 after 2.0 seconds
```

---

# How SmartRetry Works

SmartRetry is built around three core components.

## 1. RetryConfig

A configuration object responsible for:

- storing retry parameters
- validating inputs
- ensuring configuration consistency

The configuration is immutable once created.

---

## 2. Retry Engine

The internal retry engine is responsible for:

1. Running the wrapped function
2. Catching retryable exceptions
3. Waiting for a calculated delay
4. Retrying execution
5. Raising an error or calling fallback when retries are exhausted

---

## 3. Retry Decorator

The `retry` decorator wraps your function and automatically applies the retry logic when the function is executed.

Example:

```python
@retry(max_retries=3)
def my_function():
    ...
```

---

# Decorator Parameters

The retry decorator accepts several parameters that control retry behavior.

### max_retries

Type: `int`  
Default: `3`

Number of retry attempts after the first failure.

Example:

```
max_retries=3
```

Total attempts = **1 initial try + 3 retries**

---

### base_delay

Type: `float`  
Default: `1.0`

The initial delay before the first retry.

---

### backoff_factor

Type: `float`  
Default: `2.0`

Controls exponential delay growth.

---

### exceptions

Type: `tuple`  
Default:

```
(Exception,)
```

Defines which exceptions should trigger retries.

Example:

```python
exceptions=(ConnectionError, TimeoutError)
```

---

### fallback

Type: `callable`  
Default: `None`

Function to execute if all retries fail.

---

### logger

Type: `logging.Logger`  
Default: `None`

Optional logger used to record retry attempts.

---

# Exponential Backoff

SmartRetry uses exponential backoff to increase delays between retries.

Formula:

```
delay = base_delay * (backoff_factor ** attempt)
```

Example:

```
base_delay = 1
backoff_factor = 2
```

Retry delays:

```
Attempt 1 → 1 second
Attempt 2 → 2 seconds
Attempt 3 → 4 seconds
Attempt 4 → 8 seconds
```

This helps prevent overwhelming unstable systems.

---

# Using a Fallback Function

A fallback function can provide a safe result if all retries fail.

Example:

```python
from smartretry import retry

def cached_response():
    return {"status": "cached"}

@retry(max_retries=2, fallback=cached_response)
def fetch_from_api():
    raise RuntimeError("API unavailable")

result = fetch_from_api()
print(result)
```

Output:

```
{'status': 'cached'}
```

---

# Retrying Specific Exceptions Only

Sometimes you only want to retry certain types of failures.

Example:

```python
@retry(exceptions=(TimeoutError, ConnectionError))
def call_api():
    ...
```

If another exception occurs (like `TypeError`), it will be raised immediately without retrying.

---

# Custom Logger Example

You can pass your own logger to monitor retry attempts.

```python
import logging
from smartretry import retry

logger = logging.getLogger("retry")

@retry(max_retries=3, logger=logger)
def unstable_operation():
    raise RuntimeError("Failure")

unstable_operation()
```

---

# RetryExhaustedError

If all retries fail and no fallback function is provided, SmartRetry raises a custom exception.

Example:

```python
from smartretry import RetryExhaustedError

try:
    my_function()
except RetryExhaustedError as error:
    print("Attempts:", error.attempts)
    print("Last error:", error.last_error)
```

This exception provides useful debugging information.

---

# Running Tests

The repository includes a comprehensive test suite.

To run tests:

```
python test_smartretry.py
```

The tests verify:

- successful execution
- retry behavior
- exception handling
- fallback logic
- configuration validation
- delay calculation

---

# Project Structure

```
SmartRetry/
│
├── setup.py
├── test_smartretry.py
│
└── smartretry/
    ├── core.py
    └── __init__.py
```

## smartretry/core.py

Contains the main implementation:

- `RetryConfig`
- `RetryExhaustedError`
- retry execution logic
- retry decorator

---

## smartretry/__init__.py

Exposes the public API:

```python
from smartretry.core import retry, RetryExhaustedError
```

This allows users to simply write:

```python
from smartretry import retry
```

---

# Design Goals

SmartRetry was designed with the following goals:

- simplicity
- reliability
- minimal dependencies
- clear and maintainable API
- easy integration into existing projects

---

# License

This project is licensed under the **MIT License**.

---

# Contributing

Contributions are welcome.

You can help by:

- reporting bugs
- suggesting improvements
- submitting pull requests
- improving documentation

---

# Author

Ali Kamrani

GitHub  
https://github.com/MRThugh
