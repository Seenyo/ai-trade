from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .domain import BrokerOrder, OrderState

_ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PROPOSED: frozenset({OrderState.RISK_REJECTED, OrderState.APPROVED}),
    OrderState.APPROVED: frozenset({OrderState.SUBMITTING, OrderState.EXPIRED}),
    OrderState.SUBMITTING: frozenset(
        {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.RECONCILIATION_REQUIRED}
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {
            OrderState.CANCELLED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.RECONCILIATION_REQUIRED: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }
    ),
    OrderState.RISK_REJECTED: frozenset(),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


def transition_order(
    order: BrokerOrder,
    state: OrderState,
    updated_at: datetime,
    *,
    broker_order_id: str | None = None,
    filled_quantity: int | None = None,
    average_fill_price: Decimal | None = None,
) -> BrokerOrder:
    if state not in _ALLOWED_TRANSITIONS[order.state]:
        raise ValueError(f"invalid order transition: {order.state} -> {state}")
    new_filled = order.filled_quantity if filled_quantity is None else filled_quantity
    if new_filled < order.filled_quantity or new_filled > order.submitted_quantity:
        raise ValueError("invalid cumulative filled quantity")
    if state is OrderState.FILLED and new_filled != order.submitted_quantity:
        raise ValueError("filled order must have its complete submitted quantity")
    return order.model_copy(
        update={
            "state": state,
            "updated_at": updated_at,
            "broker_order_id": broker_order_id or order.broker_order_id,
            "filled_quantity": new_filled,
            "average_fill_price": average_fill_price or order.average_fill_price,
        }
    )


class OrderRegistry:
    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrder] = {}
        self._intent_keys: set[str] = set()

    def add(self, idempotency_key: str, order: BrokerOrder) -> bool:
        if idempotency_key in self._intent_keys:
            return False
        self._intent_keys.add(idempotency_key)
        self._orders[str(order.internal_order_id)] = order
        return True

    def update(self, order: BrokerOrder) -> None:
        key = str(order.internal_order_id)
        if key not in self._orders:
            raise KeyError(key)
        self._orders[key] = order

    def get(self, internal_order_id: str) -> BrokerOrder:
        return self._orders[internal_order_id]

    def all(self) -> tuple[BrokerOrder, ...]:
        return tuple(self._orders.values())
