from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .errors import RuntimeBudgetExceeded


@dataclass
class RuntimeBudget:
    """One finite budget for an interactive supervisor invocation."""

    deadline: float
    max_retries: int
    retries: int = 0

    @classmethod
    def start(cls, *, minutes: float, max_retries: int) -> RuntimeBudget:
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("runtime minutes must be finite and positive")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max retries must be a non-negative integer")
        return cls(deadline=time.monotonic() + minutes * 60, max_retries=max_retries)

    def remaining_seconds(self) -> int:
        return max(0, math.ceil(self.deadline - time.monotonic()))

    def require_time(self, *, minimum_seconds: int = 1) -> int:
        remaining = self.remaining_seconds()
        if remaining < minimum_seconds:
            raise RuntimeBudgetExceeded("The supervisor's total runtime budget is exhausted.")
        return remaining

    def consume_retry(self, reason: str) -> int:
        self.require_time()
        if self.retries >= self.max_retries:
            raise RuntimeBudgetExceeded(
                "The supervisor's retry budget is exhausted "
                f"after {self.retries} recoverable retries. Last reason: {reason}"
            )
        self.retries += 1
        return self.retries

    def sleep(self, seconds: float) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeBudgetExceeded("The supervisor's total runtime budget is exhausted.")
        time.sleep(min(seconds, remaining))
        if seconds >= remaining:
            raise RuntimeBudgetExceeded("The supervisor's total runtime budget is exhausted.")
