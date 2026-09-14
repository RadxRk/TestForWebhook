"""Shared retry policy for outbound calls to third-party services.

Both Payment.py and SnowflakeIngest.py talk to providers that fail in bursts:
a maintenance window, a rate limit, a brief network partition. Retrying those
is correct. Retrying a validation error or a declined card is not, and doing so
turns a clean failure into duplicated work.

This module draws that line once so callers do not each invent their own.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Raised by callers to mark a failure as worth another attempt. Anything not
# wrapped in this is treated as final.
class RetryableError(Exception):
    """A transient failure. Safe to attempt again."""


class PermanentError(Exception):
    """A failure that will fail identically on every retry."""


class RetriesExhausted(Exception):
    """Every attempt was used and the call still did not succeed."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"gave up after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    max_attempts counts the first call, so max_attempts=8 means one call plus
    seven retries. Delays grow base * multiplier**n, capped at max_delay, then
    a uniform random factor is applied across the whole interval. Full jitter
    matters more than it looks: without it, every worker that failed during the
    same provider outage retries in lockstep and re-creates the thundering herd
    that caused the outage.
    """

    max_attempts: int = 8
    base_delay: float = 0.5
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before `attempt` (1-indexed, so attempt 1 is 0.0)."""
        if attempt <= 1:
            return 0.0
        raw = self.base_delay * (self.multiplier ** (attempt - 2))
        capped = min(raw, self.max_delay)
        return random.uniform(0.0, capped) if self.jitter else capped

    def delays(self) -> Iterable[float]:
        """The full backoff schedule, useful for logging and tests."""
        return (self.delay_for(n) for n in range(1, self.max_attempts + 1))

    def call(self, fn: Callable[[], T], *, describe: str = "call") -> T:
        """Run fn under this policy and return its result.

        PermanentError short-circuits immediately. RetryableError and the
        exception types the caller opted into are retried until the attempt
        budget is spent.
        """
        last_error: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            wait = self.delay_for(attempt)
            if wait:
                log.info(
                    "%s: attempt %d/%d, sleeping %.2fs",
                    describe, attempt, self.max_attempts, wait,
                )
                time.sleep(wait)

            try:
                return fn()
            except PermanentError:
                # Not our call to soften. The caller said this cannot succeed.
                raise
            except RetryableError as exc:
                last_error = exc
                log.warning(
                    "%s: attempt %d/%d failed: %s",
                    describe, attempt, self.max_attempts, exc,
                )

        assert last_error is not None
        raise RetriesExhausted(self.max_attempts, last_error)


# Sensible starting points. Payments get the longer budget because a dropped
# confirmation costs a reconciliation; a warehouse batch can simply run again.
PAYMENTS_POLICY = RetryPolicy(max_attempts=10, base_delay=0.10, max_delay=50.0)
WAREHOUSE_POLICY = RetryPolicy(max_attempts=5, base_delay=2.0, max_delay=60.0)
