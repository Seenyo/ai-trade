from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from ..domain import (
    BrokerFault,
    BrokerOrder,
    Execution,
    MarketEvent,
    OrderIntent,
    OrderState,
    PortfolioSnapshot,
)


class FakeBroker:
    def __init__(
        self,
        account_id: str = "PAPER-TEST",
        starting_cash: Decimal = Decimal("25000"),
    ) -> None:
        self._account_id = account_id
        self._cash = starting_cash
        self._connected = False
        self._events: asyncio.Queue[MarketEvent | Execution | BrokerOrder | BrokerFault] = (
            asyncio.Queue()
        )
        self._counter = 0
        self.subscriptions: tuple[str, ...] = ()
        self.submitted: list[OrderIntent] = []
        self.cancelled: list[str] = []

    @property
    def account_id(self) -> str:
        return self._account_id

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def verify_paper_account(self, expected_account_id: str) -> None:
        if not self._connected:
            raise RuntimeError("broker is disconnected")
        if expected_account_id != self._account_id:
            raise RuntimeError("connected account does not match configured paper account")

    async def events(
        self,
    ) -> AsyncIterator[MarketEvent | Execution | BrokerOrder | BrokerFault]:
        while self._connected:
            yield await self._events.get()

    async def emit(self, event: MarketEvent | Execution | BrokerOrder | BrokerFault) -> None:
        await self._events.put(event)

    async def subscribe(self, instrument_ids: Sequence[str]) -> None:
        self.subscriptions = tuple(instrument_ids)

    async def submit(self, intent: OrderIntent) -> BrokerOrder:
        if not self._connected:
            raise RuntimeError("broker is disconnected")
        self._counter += 1
        self.submitted.append(intent)
        now = datetime.now(UTC)
        order = BrokerOrder(
            intent_id=intent.intent_id,
            broker_order_id=f"fake-{self._counter}",
            broker_account_id=self._account_id,
            state=OrderState.ACKNOWLEDGED,
            submitted_quantity=intent.quantity,
            created_at=now,
            updated_at=now,
        )
        await self._events.put(order)
        return order

    async def cancel(self, internal_order_id: str) -> None:
        self.cancelled.append(internal_order_id)

    async def cancel_all(self) -> None:
        self.cancelled.extend(str(item.intent_id) for item in self.submitted)

    async def account_snapshot(self) -> PortfolioSnapshot:
        now = datetime.now(UTC)
        return PortfolioSnapshot(
            captured_at=now,
            nav=self._cash,
            cash=self._cash,
            settled_cash=self._cash,
            reserved_cash=Decimal("0"),
            daily_realized_pnl=Decimal("0"),
            daily_unrealized_pnl=Decimal("0"),
        )
