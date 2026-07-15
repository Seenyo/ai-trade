from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ai_trade.domain import Bar


def test_bar_rejects_lookahead_availability(bar: Bar) -> None:
    payload = bar.model_dump()
    payload["available_at"] = bar.ended_at - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="available before"):
        Bar(**payload)


def test_bar_spread(bar: Bar) -> None:
    assert bar.spread == Decimal("0.02")
