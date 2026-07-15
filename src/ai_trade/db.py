from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .domain import Bar, BrokerOrder, Execution, MarketEvent, OrderIntent, RiskDecision


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "market_events"
    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_market_events_instrument_time", "instrument_id", "event_at"),)


class BarRow(Base):
    __tablename__ = "bars"
    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Any] = mapped_column(Numeric(24, 10), nullable=False)
    high: Mapped[Any] = mapped_column(Numeric(24, 10), nullable=False)
    low: Mapped[Any] = mapped_column(Numeric(24, 10), nullable=False)
    close: Mapped[Any] = mapped_column(Numeric(24, 10), nullable=False)
    volume: Mapped[Any] = mapped_column(Numeric(28, 8), nullable=False)
    bid: Mapped[Any | None] = mapped_column(Numeric(24, 10))
    ask: Mapped[Any | None] = mapped_column(Numeric(24, 10))
    complete: Mapped[bool] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_bars_instrument_end", "instrument_id", "ended_at"),)


class IntentRow(Base):
    __tablename__ = "order_intents"
    intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    approved: Mapped[bool] = mapped_column(nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class OrderRow(Base):
    __tablename__ = "broker_orders"
    internal_order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("broker_order_id", name="uq_broker_order_id"),
        Index("ix_broker_orders_state", "state"),
    )


class ExecutionRow(Base):
    __tablename__ = "executions"
    execution_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    broker_execution_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    internal_order_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class AuditRow(Base):
    __tablename__ = "audit_log"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class OutboxRow(Base):
    __tablename__ = "outbox"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def model_json(model: Any) -> dict[str, Any]:
    value: dict[str, Any] = model.model_dump(mode="json")
    return value


class Database:
    def __init__(self, url: str, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            yield session


class DatabaseRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_event(self, event: MarketEvent) -> None:
        async with self.database.session() as session:
            session.add(
                EventRow(
                    event_id=event.event_id,
                    instrument_id=event.instrument_id,
                    source=event.source,
                    event_type=event.event_type,
                    event_at=event.event_at,
                    received_at=event.received_at,
                    available_at=event.available_at,
                    payload=event.payload,
                )
            )

    async def save_bar(self, bar: Bar) -> None:
        async with self.database.session() as session:
            await session.merge(BarRow(**bar.model_dump()))

    async def save_intent(self, intent: OrderIntent) -> None:
        async with self.database.session() as session:
            session.add(
                IntentRow(
                    intent_id=intent.intent_id,
                    idempotency_key=intent.idempotency_key,
                    created_at=intent.created_at,
                    payload=model_json(intent),
                )
            )

    async def save_risk_decision(self, decision: RiskDecision) -> None:
        async with self.database.session() as session:
            session.add(
                RiskDecisionRow(
                    decision_id=decision.decision_id,
                    intent_id=decision.intent_id,
                    approved=decision.approved,
                    evaluated_at=decision.evaluated_at,
                    payload=model_json(decision),
                )
            )

    async def save_order(self, order: BrokerOrder) -> None:
        async with self.database.session() as session:
            await session.merge(
                OrderRow(
                    internal_order_id=order.internal_order_id,
                    intent_id=order.intent_id,
                    broker_order_id=order.broker_order_id,
                    state=order.state.value,
                    updated_at=order.updated_at,
                    payload=model_json(order),
                )
            )

    async def save_execution(self, execution: Execution) -> None:
        async with self.database.session() as session:
            session.add(
                ExecutionRow(
                    execution_id=execution.execution_id,
                    broker_execution_id=execution.broker_execution_id,
                    internal_order_id=execution.internal_order_id,
                    executed_at=execution.executed_at,
                    payload=model_json(execution),
                )
            )

    async def audit(
        self, event_at: datetime, event_type: str, actor: str, payload: dict[str, Any]
    ) -> None:
        async with self.database.session() as session:
            session.add(
                AuditRow(
                    event_at=event_at,
                    event_type=event_type,
                    actor=actor,
                    payload=payload,
                )
            )
