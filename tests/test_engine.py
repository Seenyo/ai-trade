from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_trade.brokers.fake import FakeBroker
from ai_trade.config import AppSettings, BrokerSettings
from ai_trade.domain import BrokerFault, Execution, MarketEvent, PositionEffect, SystemStatus
from ai_trade.engine import TradingEngine
from ai_trade.market import synthetic_bar
from ai_trade.memory import MemoryRepository


async def test_engine_requires_fresh_data_and_submits_once(now, buy_intent) -> None:
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    repository = MemoryRepository()
    engine = TradingEngine(settings, broker, repository, ())
    await engine.start()
    try:
        event = MarketEvent(
            instrument_id="US:SPY",
            source="test",
            event_type="PRICE",
            event_at=now,
            received_at=now,
            available_at=now,
            payload={"tick_type": 1, "price": 500},
        )
        await broker.emit(event)
        await asyncio.sleep(0)
        engine._latest_data_at = buy_intent.created_at
        await engine.arm()
        assert await engine.submit_intent(buy_intent) is not None
        assert await engine.submit_intent(buy_intent) is None
        assert len(broker.submitted) == 1
    finally:
        await engine.close()


async def test_engine_kill_cancels_all(now) -> None:
    settings = AppSettings(broker=BrokerSettings(account_id="PAPER-TEST"))
    broker = FakeBroker()
    engine = TradingEngine(settings, broker, MemoryRepository(), ())
    await engine.start()
    try:
        engine._latest_data_at = now + timedelta(days=1)
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
        engine._latest_data_at = clock
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
