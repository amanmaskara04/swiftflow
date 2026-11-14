"""Hand-written closed / open / half-open circuit breaker."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from prometheus_client import Gauge

CIRCUIT_STATE = Gauge("swiftflow_circuit_state", "Circuit state: 0 closed, 1 half-open, 2 open", ["name"])

T = TypeVar("T")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"
_STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_in: float) -> None:
        super().__init__(f"circuit {name} is open; retry in {retry_in:.1f}s")
        self.retry_in = retry_in


class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures. After
    ``reset_timeout`` seconds one probe call is let through (half-open): success
    closes the circuit, failure opens it again."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._publish()

    @property
    def state(self) -> str:
        if self._state == OPEN and self.clock() - self._opened_at >= self.reset_timeout:
            return HALF_OPEN
        return self._state

    def retry_in(self) -> float:
        if self._state != OPEN:
            return 0.0
        return max(0.0, self.reset_timeout - (self.clock() - self._opened_at))

    def allow(self) -> bool:
        state = self.state
        if state == CLOSED:
            return True
        if state == HALF_OPEN:
            if self._probe_in_flight:
                return False
            self._state = HALF_OPEN
            self._probe_in_flight = True
            self._publish()
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._probe_in_flight = False
        self._state = CLOSED
        self._publish()

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self._state == HALF_OPEN:
            self._open()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = OPEN
        self._opened_at = self.clock()
        self._publish()

    def _publish(self) -> None:
        CIRCUIT_STATE.labels(self.name).set(_STATE_VALUE[self._state])

    async def call(self, fn: Callable[[], Awaitable[T]], is_failure: Callable[[Any], bool] | None = None) -> T:
        if not self.allow():
            raise CircuitOpenError(self.name, self.retry_in())
        try:
            result = await fn()
        except Exception:
            self.record_failure()
            raise
        if is_failure is not None and is_failure(result):
            self.record_failure()
        else:
            self.record_success()
        return result

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "failures": self._failures}
