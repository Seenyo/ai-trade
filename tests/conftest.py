from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_trade.domain import Bar, OrderIntent, PositionEffect, Side


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 15, 15, 0, tzinfo=UTC)


@pytest.fixture
def bar(now: datetime) -> Bar:
    return Bar(
        instrument_id="US:AAPL",
        started_at=now - timedelta(minutes=1),
        ended_at=now,
        available_at=now,
        open=Decimal("200"),
        high=Decimal("201"),
        low=Decimal("199"),
        close=Decimal("200.50"),
        volume=Decimal("100000"),
        bid=Decimal("200.49"),
        ask=Decimal("200.51"),
    )


@pytest.fixture
def buy_intent(now: datetime) -> OrderIntent:
    return OrderIntent(
        idempotency_key="test-aapl-entry",
        strategy="momentum",
        instrument_id="US:AAPL",
        side=Side.BUY,
        effect=PositionEffect.OPEN,
        quantity=10,
        reference_price=Decimal("200"),
        limit_price=Decimal("200.10"),
        stop_price=Decimal("198.50"),
        target_price=Decimal("203"),
        created_at=now,
        expires_at=now + timedelta(seconds=10),
    )
