from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionEffect(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class OrderState(StrEnum):
    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class SystemStatus(StrEnum):
    DISARMED = "DISARMED"
    ARMING = "ARMING"
    ARMED = "ARMED"
    KILLED = "KILLED"


class Instrument(FrozenModel):
    instrument_id: str
    symbol: str
    exchange: str = "SMART"
    primary_exchange: str
    currency: str = "USD"
    security_type: str = "STK"
    minimum_tick: Decimal = Decimal("0.01")
    lot_size: int = 1
    eligible_from: datetime | None = None
    eligible_to: datetime | None = None


class MarketEvent(FrozenModel):
    event_id: UUID = Field(default_factory=uuid4)
    instrument_id: str
    source: str
    event_type: str
    event_at: datetime
    received_at: datetime
    available_at: datetime
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_timestamps(self) -> MarketEvent:
        if self.available_at < self.event_at:
            raise ValueError("available_at cannot precede event_at")
        return self


class Bar(FrozenModel):
    instrument_id: str
    interval_seconds: int = 60
    started_at: datetime
    ended_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    complete: bool = True

    @model_validator(mode="after")
    def validate_bar(self) -> Bar:
        if self.started_at >= self.ended_at:
            raise ValueError("bar start must precede bar end")
        if self.available_at < self.ended_at:
            raise ValueError("bar cannot be available before it ends")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("crossed quote")
        return self

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class FeatureSnapshot(FrozenModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    strategy: str
    instrument_id: str
    decision_at: datetime
    feature_set_version: str
    values: dict[str, float]
    missing: tuple[str, ...] = ()


class SignalProposal(FrozenModel):
    signal_id: UUID = Field(default_factory=uuid4)
    strategy: str
    model_version: str
    instrument_id: str
    side: Side
    confidence: float = Field(ge=0, le=1)
    expected_return_bps: Decimal
    estimated_cost_bps: Decimal
    horizon_seconds: int
    created_at: datetime
    expires_at: datetime
    feature_snapshot_id: UUID

    @model_validator(mode="after")
    def validate_expiry(self) -> SignalProposal:
        if self.expires_at <= self.created_at:
            raise ValueError("signal must expire after creation")
        return self


class OrderIntent(FrozenModel):
    intent_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str
    signal_id: UUID | None = None
    strategy: str
    instrument_id: str
    side: Side
    effect: PositionEffect
    quantity: int
    reference_price: Decimal
    limit_price: Decimal
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    created_at: datetime
    expires_at: datetime

    @field_validator("quantity")
    @classmethod
    def whole_positive_quantity(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("quantity must be a positive whole number")
        return value


class RiskDecision(FrozenModel):
    decision_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    approved: bool
    reason_codes: tuple[str, ...] = ()
    evaluated_at: datetime
    configuration_version: str
    checks: dict[str, str] = Field(default_factory=dict)


class BrokerOrder(FrozenModel):
    internal_order_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    broker_order_id: str | None = None
    broker_account_id: str
    state: OrderState = OrderState.PROPOSED
    submitted_quantity: int
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
    created_at: datetime
    updated_at: datetime


class Execution(FrozenModel):
    execution_id: UUID = Field(default_factory=uuid4)
    broker_execution_id: str
    internal_order_id: UUID
    strategy: str
    instrument_id: str
    side: Side
    quantity: int
    price: Decimal
    commission: Decimal = Decimal("0")
    executed_at: datetime


class BrokerFault(FrozenModel):
    code: int
    message: str
    request_id: int = -1
    fatal: bool = True
    occurred_at: datetime


class PositionLot(FrozenModel):
    strategy: str
    instrument_id: str
    quantity: int
    average_price: Decimal
    market_price: Decimal

    @property
    def market_value(self) -> Decimal:
        return Decimal(self.quantity) * self.market_price


class PortfolioSnapshot(FrozenModel):
    captured_at: datetime
    nav: Decimal
    cash: Decimal
    settled_cash: Decimal
    reserved_cash: Decimal
    daily_realized_pnl: Decimal
    daily_unrealized_pnl: Decimal
    positions: tuple[PositionLot, ...] = ()

    @property
    def gross_exposure(self) -> Decimal:
        return sum((abs(position.market_value) for position in self.positions), Decimal("0"))


class OperatorCommand(FrozenModel):
    command_id: UUID = Field(default_factory=uuid4)
    command: str
    actor: str
    reason: str
    created_at: datetime


class SystemState(FrozenModel):
    status: SystemStatus = SystemStatus.DISARMED
    trading_day: str | None = None
    paper_account_id: str | None = None
    reason: str | None = None
    updated_at: datetime
