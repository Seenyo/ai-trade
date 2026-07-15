from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol

from .domain import (
    Bar,
    BrokerFault,
    BrokerOrder,
    Execution,
    FeatureSnapshot,
    MarketEvent,
    OrderIntent,
    PortfolioSnapshot,
    RiskDecision,
    SignalProposal,
)


class BrokerPort(Protocol):
    @property
    def account_id(self) -> str: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def verify_paper_account(self, expected_account_id: str) -> None: ...

    def events(self) -> AsyncIterator[MarketEvent | Execution | BrokerOrder | BrokerFault]: ...

    async def subscribe(self, instrument_ids: Sequence[str]) -> None: ...

    async def submit(self, intent: OrderIntent) -> BrokerOrder: ...

    async def cancel(self, internal_order_id: str) -> None: ...

    async def cancel_all(self) -> None: ...

    async def account_snapshot(self) -> PortfolioSnapshot: ...


class MarketDataPort(Protocol):
    async def latest_bar(self, instrument_id: str) -> Bar | None: ...

    async def bars(self, instrument_id: str, start: datetime, end: datetime) -> Sequence[Bar]: ...


class StrategyPort(Protocol):
    name: str

    def propose(self, snapshot: FeatureSnapshot) -> SignalProposal | None: ...


class RiskPort(Protocol):
    def evaluate(
        self,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        now: datetime,
        latest_data_at: datetime | None,
        system_armed: bool,
        pending_entries: Sequence[OrderIntent] = (),
    ) -> RiskDecision: ...


class ExecutionPort(Protocol):
    async def execute(self, intent: OrderIntent) -> BrokerOrder: ...


class RepositoryPort(Protocol):
    async def save_event(self, event: MarketEvent) -> None: ...

    async def save_bar(self, bar: Bar) -> None: ...

    async def save_intent(self, intent: OrderIntent) -> None: ...

    async def save_risk_decision(self, decision: RiskDecision) -> None: ...

    async def save_order(self, order: BrokerOrder) -> None: ...

    async def save_execution(self, execution: Execution) -> None: ...
