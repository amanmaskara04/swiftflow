"""The order state machine. Pure functions: no I/O, fully unit-tested."""

from __future__ import annotations

PENDING, RESERVED, CONFIRMED, SHIPPED, REJECTED, CANCELLED = (
    "PENDING",
    "RESERVED",
    "CONFIRMED",
    "SHIPPED",
    "REJECTED",
    "CANCELLED",
)
STATUSES = (PENDING, RESERVED, CONFIRMED, SHIPPED, REJECTED, CANCELLED)
TERMINAL = frozenset({SHIPPED, REJECTED, CANCELLED})

TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({RESERVED, REJECTED, CANCELLED}),
    RESERVED: frozenset({CONFIRMED, CANCELLED}),
    CONFIRMED: frozenset({SHIPPED, CANCELLED}),
    SHIPPED: frozenset(),
    REJECTED: frozenset(),
    CANCELLED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def can_cancel(status: str) -> bool:
    return can_transition(status, CANCELLED)


def can_ship(status: str) -> bool:
    return can_transition(status, SHIPPED)


def payment_status_after_cancel(status: str, payment_status: str, payment_attempts: int) -> str:
    """What must happen to the money when an order is cancelled.

    - already charged                      -> refund it
    - RESERVED with a charge attempt made  -> the charge may be in flight or may have
      succeeded without us hearing back, so void/refund by the order's idempotency key
    - otherwise                            -> nothing to do
    """
    if payment_status == "CHARGED":
        return "REFUND_PENDING"
    if status == RESERVED and payment_attempts > 0:
        return "REFUND_PENDING"
    return payment_status


def retry_delay_seconds(attempts: int, cap: float = 30.0) -> float:
    """Backoff for payment retries: 1, 2, 4 ... capped."""
    return float(min(cap, 2 ** max(0, min(attempts, 10) - 1)))
