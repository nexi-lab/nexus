"""Retry Utilities with Exponential Backoff.

This module provides decorators and utilities for retrying failed operations
with configurable backoff strategies.

Author: Alex Turner
Created: January 2024

Retry Strategies:
- Exponential backoff: delay = base_delay * (2 ^ attempt)
- Linear backoff: delay = base_delay * attempt
- Constant delay: delay = base_delay

Default Configuration:
- Maximum retries: 3
- Base delay: 1 second
- Maximum delay: 30 seconds
- Jitter: 0-500ms random
"""

import asyncio
import logging
import random
from collections.abc import Callable
from functools import wraps

logger = logging.getLogger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0
DEFAULT_JITTER_MAX = 0.5


class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, message: str, last_exception: Exception):
        super().__init__(message)
        self.last_exception = last_exception


def calculate_exponential_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter_max: float = DEFAULT_JITTER_MAX,
) -> float:
    """Calculate delay with exponential backoff and jitter.

    Formula: min(base_delay * 2^attempt + random(0, jitter), max_delay)
    """
    delay = min(base_delay * (2**attempt), max_delay)
    jitter = random.uniform(0, jitter_max)
    return delay + jitter


def retry(
    max_retries: int = DEFAULT_MAX_RETRIES,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
):
    """Decorator for retrying synchronous functions.

    Example:
        @retry(max_retries=3, exceptions=(ConnectionError,))
        def fetch_data():
            return requests.get(url)
    """

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = calculate_exponential_delay(attempt, base_delay, max_delay)
                        logger.warning(
                            f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                            f"after {delay:.2f}s: {e}"
                        )
                        import time

                        time.sleep(delay)
            raise RetryError(
                f"All {max_retries} retries exhausted for {func.__name__}", last_exception
            )

        return wrapper

    return decorator


def async_retry(
    max_retries: int = DEFAULT_MAX_RETRIES,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
):
    """Decorator for retrying async functions.

    Example:
        @async_retry(max_retries=5, exceptions=(aiohttp.ClientError,))
        async def fetch_data():
            async with session.get(url) as response:
                return await response.json()
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = calculate_exponential_delay(attempt, base_delay, max_delay)
                        logger.warning(
                            f"Async retry {attempt + 1}/{max_retries} for {func.__name__} "
                            f"after {delay:.2f}s: {e}"
                        )
                        await asyncio.sleep(delay)
            raise RetryError(
                f"All {max_retries} retries exhausted for {func.__name__}", last_exception
            )

        return wrapper

    return decorator
