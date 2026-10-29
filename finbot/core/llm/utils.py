"""
Utility helpers for the LLM layer.

This module provides retry and backoff functionality that can be reused
by all LLM providers (OpenAI, Ollama, etc.).
"""

import asyncio
import functools
import logging
from typing import Callable, Tuple, Type

logger = logging.getLogger(__name__)

# Transient errors
RETRYABLE_ERRORS: Tuple[Type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
)


def retry(
    max_retries: int = 3,
    backoff_seconds: float = 0.5,
) -> Callable:
    """
    Decorator for async functions to retry on transient errors.

    Args:
        max_retries (int): Maximum retry attempts.
        backoff_seconds (float): Base delay before retrying.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0

            while True:
                try:
                    return await func(*args, **kwargs)

                except RETRYABLE_ERRORS as exc:
                    attempt += 1

                    if attempt > max_retries:
                        raise

                    sleep_time = backoff_seconds * (2 ** (attempt - 1))

                    logger.warning(
                        "[%s] Retry %d/%d in %.2fs due to: %s",
                        func.__name__,
                        attempt,
                        max_retries,
                        sleep_time,
                        exc,
                    )

                    await asyncio.sleep(sleep_time)

        return wrapper

    return decorator
