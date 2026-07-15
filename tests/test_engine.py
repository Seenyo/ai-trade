from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from ai_trade.brokers.fake import FakeBroker
from ai_trade.config import AppSettings, BrokerSettings, StrategySettings
from ai_trade.domain import (
    BrokerFault,
    Execution,
    MarketEvent,
    PositionEffect,
    Side,
    SystemStatus,
)
from ai_trade.engine import TradingEngine
from ai_trade.market import synthetic_bar
from ai_trade.memory import MemoryRepository
from ai_trade.risk import RiskCode


class _RecordingEngine(TradingEngine):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.decisions: list[datetime] = []

    async def _decision_cycle(self, decision_at: datetime) -> None:
        self.decisions.append(decision_at)


async def test_engine_requires_fresh_data_and_submits_once(buy_intent) -> None:
    clock = datetime.now(UTC)
    fresh_intent = buy_intent.model_copy(
        update={
            "created_at": clock,
            "expires_at": clock + timedelta(seconds=30),
        }
    )
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    repository = MemoryRepository()
    engine = TradingEngine(settings, broker, repository, ())
    await engine.start()
    try:
        for instrument_id in ("US:AAPL", "US:SPY", "US:QQQ"):
            await broker.emit(
                MarketEvent(
                    instrument_id=instrument_id,
                    source="test",
                    event_type="PRICE",
                    event_at=clock,
                    received_at=clock,
                    available_at=clock,
                    payload={"tick_type": 1, "price": 500},
                )
            )
        await asyncio.sleep(0.01)
        await engine.arm()
        assert await engine.submit_intent(fresh_intent) is not None
        assert await engine.submit_intent(fresh_intent) is None
        assert len(broker.submitted) == 1
    finally:
        await engine.close()


async def test_arm_requires_fresh_spy_and_qqq() -> None:
    clock = datetime.now(UTC)
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    engine = TradingEngine(settings, broker, MemoryRepository(), ())
    await engine.start()
    try:
        engine._latest_data_by_instrument["US:SPY"] = clock
        with pytest.raises(RuntimeError, match="US:QQQ"):
            await engine.arm()
    finally:
        await engine.close()


async def test_intent_risk_requires_fresh_candidate_feed(buy_intent) -> None:
    clock = datetime.now(UTC)
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    repository = MemoryRepository()
    engine = TradingEngine(settings, broker, repository, ())
    await engine.start()
    try:
        engine._latest_data_by_instrument.update(
            {instrument_id: clock for instrument_id in ("US:SPY", "US:QQQ")}
        )
        await engine.arm()
        intent = buy_intent.model_copy(
            update={
                "created_at": clock,
                "expires_at": clock + timedelta(seconds=30),
            }
        )

        assert await engine.submit_intent(intent) is None
        assert RiskCode.STALE_DATA in repository.decisions[-1].reason_codes
        assert not broker.submitted
    finally:
        await engine.close()


async def test_engine_kill_cancels_all() -> None:
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    engine = TradingEngine(settings, broker, MemoryRepository(), ())
    await engine.start()
    try:
        await engine.kill("fault drill")
        assert engine.state.status.value == "KILLED"
    finally:
        await engine.close()


async def test_engine_constructs_controlled_time_exit(buy_intent) -> None:
    clock = datetime.now(UTC)
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    engine = TradingEngine(settings, broker, MemoryRepository(), ())
    await engine.start()
    try:
        engine._latest_data_by_instrument.update(
            {instrument_id: clock for instrument_id in ("US:AAPL", "US:SPY", "US:QQQ")}
        )
        await engine._set_state(SystemStatus.ARMED, "test")
        entry_intent = buy_intent.model_copy(
            update={
                "created_at": clock,
                "expires_at": clock + timedelta(seconds=30),
            }
        )
        order = await engine.submit_intent(entry_intent)
        assert order is not None
        await broker.emit(
            Execution(
                broker_execution_id="entry-fill",
                internal_order_id=order.internal_order_id,
                strategy=entry_intent.strategy,
                instrument_id=entry_intent.instrument_id,
                side=entry_intent.side,
                quantity=entry_intent.quantity,
                price=Decimal("200"),
                commission=Decimal("1"),
                executed_at=clock - timedelta(minutes=61),
            )
        )
        await asyncio.sleep(0.01)
        engine.bars.append(
            synthetic_bar(
                entry_intent.instrument_id,
                clock - timedelta(minutes=1),
                Decimal("201"),
                Decimal("10000"),
            )
        )

        await engine._exit_due_positions(clock, force_all=False)

        assert str(order.internal_order_id) in broker.cancelled
        assert broker.submitted[-1].effect is PositionEffect.CLOSE
        assert broker.submitted[-1].quantity == entry_intent.quantity
    finally:
        await engine.close()


async def test_fatal_broker_fault_kills_engine() -> None:
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    engine = TradingEngine(settings, broker, MemoryRepository(), ())
    await engine.start()
    try:
        await broker.emit(
            BrokerFault(
                code=1100,
                message="connectivity lost",
                occurred_at=datetime.now(UTC),
            )
        )
        await asyncio.sleep(0.01)
        assert engine.state.status is SystemStatus.KILLED
    finally:
        await engine.close()


async def test_watchdog_detects_stale_open_position_feed() -> None:
    clock = datetime.now(UTC)
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    engine = TradingEngine(settings, broker, MemoryRepository(), ())
    await engine.start()
    try:
        engine._latest_data_by_instrument.update(
            {
                "US:AAPL": clock - timedelta(seconds=20),
                "US:SPY": clock,
                "US:QQQ": clock,
            }
        )
        engine.ledger.apply_execution(
            Execution(
                broker_execution_id="unmanaged-test-fill",
                internal_order_id=uuid4(),
                strategy="momentum",
                instrument_id="US:AAPL",
                side=Side.BUY,
                quantity=1,
                price=Decimal("200"),
                executed_at=clock,
            )
        )
        await engine._set_state(SystemStatus.ARMED, "test")

        for _ in range(20):
            if engine.state.status is SystemStatus.KILLED:
                break
            await asyncio.sleep(0.1)

        assert engine.state.status is SystemStatus.KILLED
        assert "US:AAPL" in (engine.state.reason or "")
    finally:
        await engine.close()


async def test_decision_waits_until_all_required_bars_arrive(now) -> None:
    settings = AppSettings(
        broker=BrokerSettings(account_id="PAPER-TEST"),
        strategy=StrategySettings(decision_collection_timeout_seconds=1.0),
    )
    engine = _RecordingEngine(settings, FakeBroker(), MemoryRepository(), ())
    engine.symbols = ("US:AAPL",)
    engine.indicators = ("US:SPY", "US:QQQ")
    await engine._set_state(SystemStatus.ARMED, "test")

    for instrument_id in ("US:AAPL", "US:SPY"):
        bar = synthetic_bar(
            instrument_id,
            now - timedelta(minutes=1),
            Decimal("100"),
            Decimal("10000"),
        )
        engine.bars.append(bar)
        engine._register_decision_bar(bar)
    await asyncio.sleep(0)
    assert not engine.decisions

    qqq = synthetic_bar(
        "US:QQQ",
        now - timedelta(minutes=1),
        Decimal("100"),
        Decimal("10000"),
    )
    engine.bars.append(qqq)
    engine._register_decision_bar(qqq)
    for _ in range(20):
        if engine.decisions:
            break
        await asyncio.sleep(0.01)

    assert engine.decisions == [now]


async def test_decision_barrier_has_bounded_timeout(now) -> None:
    settings = AppSettings(
        broker=BrokerSettings(account_id="PAPER-TEST"),
        strategy=StrategySettings(decision_collection_timeout_seconds=0.02),
    )
    engine = _RecordingEngine(settings, FakeBroker(), MemoryRepository(), ())
    engine.symbols = ("US:AAPL",)
    engine.indicators = ("US:SPY", "US:QQQ")
    await engine._set_state(SystemStatus.ARMED, "test")
    aapl = synthetic_bar(
        "US:AAPL",
        now - timedelta(minutes=1),
        Decimal("100"),
        Decimal("10000"),
    )
    engine.bars.append(aapl)
    engine._register_decision_bar(aapl)

    await asyncio.sleep(0.04)

    assert engine.decisions == [now]
    timeout_events = [
        event
        for event in engine.audit_log()
        if event["event_type"] == "DECISION_COLLECTION_TIMEOUT"
    ]
    assert timeout_events
    assert timeout_events[-1]["payload"]["missing"] == ["US:QQQ", "US:SPY"]
