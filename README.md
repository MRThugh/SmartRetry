<div align="center">
  
  # 🚀 SmartRetry
  
  **The Ultimate Resilience Arsenal for Python Applications.**

  [![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg?style=for-the-badge&logo=python)](https://www.python.org/)
  [![Version](https://img.shields.io/badge/version-1.0.0-success.svg?style=for-the-badge)](#)
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
  [![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg?style=for-the-badge)](#)
  
  *Built with ❤️ by [Ali Kamrani](https://github.com/MRThugh)*

  <p align="center">
    Stop letting flaky APIs, unstable database connections, and temporary network drops crash your applications. <b>SmartRetry</b> is a pure-Python, zero-dependency, ultra-lightweight library that wraps your functions in a bulletproof vest of exponential backoffs and smart fallbacks.
  </p>

</div>

---

## 📑 Table of Contents
- [✨ Why SmartRetry?](#-why-smartretry)
- [📦 Installation](#-installation)
- [⚡ Quick Start](#-quick-start)
- [🛠️ Core Features & Usage](#️-core-features--usage)
  - [1. Exponential Backoff](#1-exponential-backoff)
  - [2. Precise Exception Filtering](#2-precise-exception-filtering)
  - [3. The Ultimate Safety Net: Fallbacks](#3-the-ultimate-safety-net-fallbacks)
- [🧮 How The Math Works](#-how-the-math-works)
- [📚 API Reference](#-api-reference)
- [🧪 Comprehensive Testing](#-comprehensive-testing)
- [📄 License](#-license)

---

## ✨ Why SmartRetry?

Most retry libraries are either too complex, heavily bloated with third-party dependencies, or lack proper type safety. **SmartRetry** is different:

- **Zero Dependencies:** Built entirely on standard Python libraries.
- **Fail-Fast Design:** Only retry the exact exceptions you want. Everything else crashes immediately, saving you compute time.
- **Eager Validation:** Configuration is validated at decoration time, not execution time. If you misconfigure it, your app won't even boot.
- **Seamless Fallbacks:** If the server is truly dead, seamlessly route the user to offline/cached data without throwing an error.
- **Silent Observability:** Integrated natively with Python's `logging` module out of the box.

---

## 📦 Installation

Since SmartRetry is zero-dependency, installation is lightning fast.

Clone the repository and install it locally:
```bash
git clone https://github.com/MRThugh/SmartRetry.git
cd SmartRetry
pip install -e
```
---

## ⚡ Quick Start

Just import the `@retry` decorator and slap it onto any flaky function!

python
import random
from smartretry import retry

@retry(max_retries=3, base_delay=1.0)
def fetch_data():
if random.random() < 0.7:
raise ConnectionError("Network blip!")
return "✅ Data fetched successfully!"

print(fetch_data())
*SmartRetry catches the `ConnectionError`, waits, and tries again automatically.*

---

## 🛠️ Core Features & Usage

### 1. Exponential Backoff
Slamming a broken server with immediate retries makes the problem worse. SmartRetry uses exponential backoff to give servers breathing room.

python
@retry(max_retries=4, base_delay=0.5, backoff_factor=2.0)
def call_heavy_api():
# Attempt 1: Fails -> Waits 0.5s
# Attempt 2: Fails -> Waits 1.0s
# Attempt 3: Fails -> Waits 2.0s
# Attempt 4: Fails -> Waits 4.0s
pass

### 2. Precise Exception Filtering
Don't retry a `KeyError` or an `AuthenticationError` 100 times. Tell SmartRetry *exactly* what to forgive.

python
class NetworkTimeout(Exception): pass
class AuthError(Exception): pass

# ONLY retry on NetworkTimeout. If AuthError happens, it crashes immediately!
@retry(max_retries=3, exceptions=(NetworkTimeout,))
def login(user, password):
pass

### 3. The Ultimate Safety Net: Fallbacks
What if all retries fail? Instead of crashing the user's app, route them to a fallback function. 
**Rule:** *Your fallback must accept the exact same arguments as your original function.*

python
def load_cached_weather(city: str) -> dict:
print(f"⚠️ API down. Serving cached data for {city}.")
return {"city": city, "temp": "Unknown (Offline)"}

@retry(max_retries=3, exceptions=(TimeoutError,), fallback=load_cached_weather)
def get_live_weather(city: str) -> dict:
raise TimeoutError("Server is completely dead!")

# This will fail 3 times, then silently return the cached data!
data = get_live_weather("Tehran") 

---

## 🧮 How The Math Works

SmartRetry calculates the wait time before each retry attempt using the following formula:

$$delay = base\_delay \times backoff\_factor^{attempt}$$

*(Note: The `attempt` index is zero-based. The first retry is attempt 0).*

**Example with `base_delay=2.0` and `backoff_factor=3.0`:**
- **Retry 1 (Attempt 0):** $2.0 \times 3.0^0 = 2.0$ seconds
- **Retry 2 (Attempt 1):** $2.0 \times 3.0^1 = 6.0$ seconds
- **Retry 3 (Attempt 2):** $2.0 \times 3.0^2 = 18.0$ seconds

---

## 📚 API Reference

### `@retry(...)`

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `max_retries` | `int` | `3` | Maximum extra attempts after the first failure. Must be $\ge 0$. |
| `base_delay` | `float` | `1.0` | Seconds to wait before the *first* retry. Must be $\ge 0$. |
| `backoff_factor`| `float` | `2.0` | Exponential multiplier applied each round. Must be $\ge 1.0$. |
| `exceptions` | `Tuple` | `(Exception,)` | Tuple of Exception classes to catch. Unlisted exceptions crash immediately. |
| `fallback` | `Callable` | `None` | Function to call if all retries fail. Must share the original function's signature. |
| `logger` | `Logger` | `None` | Custom Python logger. Defaults to `smartretry.core`. |

### `RetryExhaustedError`
Raised when a function fails on every single attempt and no `fallback` is provided. Contains attributes:
- `attempts`: Total number of attempts made.
- `last_error`: The final exception that triggered the failure.
- `func_name`: The name of the function that failed.

---

## 🧪 Comprehensive Testing
SmartRetry comes with a hardcore, isolated test suite covering statistical probabilities, delay math, eager validations, and metadata preservation. 

To run the tests:
bash
python test_smartretry.py

---

## 📄 License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for more details.

---
<div align="center">
  <i>"Make your code bulletproof."</i><br>
  <b>— Ali Kamrani</b>
</div>
