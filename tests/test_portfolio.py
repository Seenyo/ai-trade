from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from ai_trade.domain import Execution, Side
from ai_trade.portfolio import PortfolioLedger


def execution(
    execution_id: str,
    side: Side,
    quantity: int,
    price: str,
    now: datetime,
) -> Execution:
    return Execution(
        broker_execution_id=execution_id,
        internal_order_id=uuid4(),
        strategy="momentum",
        instrument_id="US:AAPL",
        side=side,
        quantity=quantity,
        price=Decimal(price),
        commission=Decimal("1"),
        executed_at=now,
    )


def test_execution_is_idempotent_and_sleeve_owned(now: datetime) -> None:
    ledger = PortfolioLedger(Decimal("25000"))
    buy = execution("buy-1", Side.BUY, 10, "100", now)
    assert ledger.apply_execution(buy)
    assert not ledger.apply_execution(buy)
    assert ledger.quantity("momentum", "US:AAPL") == 10
    assert ledger.snapshot(now).cash == Decimal("23999")

    sell = execution("sell-1", Side.SELL, 10, "102", now)
    assert ledger.apply_execution(sell)
    assert ledger.quantity("momentum", "US:AAPL") == 0
    assert ledger.snapshot(now).daily_realized_pnl == Decimal("19")


def test_sell_cannot_exceed_sleeve_quantity(now: datetime) -> None:
    ledger = PortfolioLedger(Decimal("25000"))
    with pytest.raises(ValueError, match="sleeve-owned"):
        ledger.apply_execution(execution("sell", Side.SELL, 1, "100", now))


def test_sale_proceeds_remain_unsettled(now: datetime) -> None:
    ledger = PortfolioLedger(Decimal("25000"))
    ledger.apply_execution(execution("buy", Side.BUY, 10, "100", now))
    ledger.apply_execution(execution("sell", Side.SELL, 10, "100", now))
    snapshot = ledger.snapshot(now)
    assert snapshot.cash > snapshot.settled_cash
    ledger.settle_cash()
    assert ledger.snapshot(now).cash == ledger.snapshot(now).settled_cash
