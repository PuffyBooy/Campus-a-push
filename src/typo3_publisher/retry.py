"""Retry with exponential backoff and jitter."""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import TypeVar

from .config import RetryPolicy
from .logging_utils import get_logger

T = TypeVar("T")
log = get_logger("retry")

# Tests replace this to avoid real waiting.
sleep = time.sleep


def backoff_delay(attempt: int, policy: RetryPolicy, *, jitter: bool = True) -> float:
    """Delay before retry number ``attempt`` (1-based)."""
    delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** (attempt - 1)))
    if jitter:
        delay *= random.uniform(0.8, 1.2)
    return max(0.0, delay)


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (),
    description: str = "operation",
) -> T:
    attempts = max(1, policy.attempts)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except give_up_on:
            raise
        except retry_on as exc:
            if attempt >= attempts:
                log.error("%s failed after %d attempt(s): %s", description, attempt, exc)
                raise
            delay = backoff_delay(attempt, policy)
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs", description, attempt, attempts, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def retrying(policy: RetryPolicy, **kwargs):
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kw):
            return retry_call(lambda: fn(*args, **kw), policy, description=fn.__qualname__, **kwargs)

        return wrapper

    return decorator
