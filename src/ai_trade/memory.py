from __future__ import annotations

from .domain import Bar, BrokerOrder, Execution, MarketEvent, OrderIntent, RiskDecision


class MemoryRepository:
    """Test and no-database repository with the same write contract."""

    def __init__(self) -> None:
        self.events: list[MarketEvent] = []
        self.bars: list[Bar] = []
        self.intents: list[OrderIntent] = []
        self.decisions: list[RiskDecision] = []
        self.orders: list[BrokerOrder] = []
        self.executions: list[Execution] = []

    async def save_event(self, event: MarketEvent) -> None:
        self.events.append(event)

    async def save_bar(self, bar: Bar) -> None:
        self.bars.append(bar)

    async def save_intent(self, intent: OrderIntent) -> None:
        self.intents.append(intent)

    async def save_risk_decision(self, decision: RiskDecision) -> None:
        self.decisions.append(decision)

    async def save_order(self, order: BrokerOrder) -> None:
        self.orders.append(order)

    async def save_execution(self, execution: Execution) -> None:
        self.executions.append(execution)
