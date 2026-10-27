"""An absolute, monotonic request deadline.

The deadline is computed once per request and never reset, so every blocking operation sees
what is left of the same budget. Enforcement is cooperative: operations are given a timeout
no larger than the time remaining and results are rejected if they arrive late. A timeout
does not cancel work already running elsewhere (see docs/iterative-orchestration.md).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


class DeadlineExceeded(TimeoutError):
    """The request deadline passed. Distinct from an ordinary service or planner failure."""


@dataclass(frozen=True)
class RequestDeadline:
    expires_at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, clock: Callable[[], float] = time.monotonic) -> "RequestDeadline":
        return cls(expires_at=clock() + seconds, clock=clock)

    def remaining(self) -> float:
        """Seconds left. Zero or negative once the deadline has passed."""
        return self.expires_at - self.clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def require_remaining(self) -> float:
        """Seconds left, or DeadlineExceeded if an operation tries to begin after expiry."""
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("request deadline exceeded")
        return remaining


def bounded_timeout(cap: float, deadline: RequestDeadline | None) -> float:
    """min(cap, time remaining), with no floor. Raises if the deadline has already passed."""
    return cap if deadline is None else min(cap, deadline.require_remaining())


def is_timeout_error(exc: BaseException) -> bool:
    """True for timeout-shaped errors from urllib, botocore, httpx and LiteLLM."""
    if isinstance(exc, TimeoutError):
        return True
    chain = [exc, exc.__cause__, exc.__context__]
    for item in chain:
        if item is None:
            continue
        name = type(item).__name__
        if "Timeout" in name:
            return True
        reason = getattr(item, "reason", None)  # urllib.error.URLError(reason=timeout(...))
        if isinstance(reason, TimeoutError):
            return True
    return False
