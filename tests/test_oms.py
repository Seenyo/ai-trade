from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from ai_trade.domain import BrokerOrder, OrderState
from ai_trade.oms import OrderRegistry, transition_order


def proposed(now: datetime) -> BrokerOrder:
    from uuid import uuid4

    return BrokerOrder(
        intent_id=uuid4(),
        broker_account_id="PAPER",
        submitted_quantity=10,
        created_at=now,
        updated_at=now,
    )


def test_order_happy_path(now: datetime) -> None:
    order = transition_order(proposed(now), OrderState.APPROVED, now)
    order = transition_order(order, OrderState.SUBMITTING, now)
    order = transition_order(order, OrderState.ACKNOWLEDGED, now, broker_order_id="123")
    order = transition_order(
        order,
        OrderState.PARTIALLY_FILLED,
        now,
        filled_quantity=4,
        average_fill_price=Decimal("100"),
    )
    order = transition_order(order, OrderState.FILLED, now, filled_quantity=10)
    assert order.state is OrderState.FILLED


def test_invalid_transition_is_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="invalid order transition"):
        transition_order(proposed(now), OrderState.FILLED, now, filled_quantity=10)


def test_registry_rejects_duplicate_idempotency_key(now: datetime) -> None:
    registry = OrderRegistry()
    assert registry.add("stable-key", proposed(now))
    assert not registry.add("stable-key", proposed(now))
