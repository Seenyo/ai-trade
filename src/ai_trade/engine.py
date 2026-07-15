from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppSettings, TradingMode
from .domain import (
    Bar,
    BrokerFault,
    BrokerOrder,
    Execution,
    MarketEvent,
    OperatorCommand,
    OrderIntent,
    OrderState,
    PositionEffect,
    Side,
    SignalProposal,
    SystemState,
    SystemStatus,
)
from .market import BarStore, MinuteBarAggregator, build_features
from .oms import OrderRegistry, transition_order
from .portfolio import PortfolioLedger
from .ports import BrokerPort, RepositoryPort, StrategyPort
from .risk import RiskEngine, size_whole_shares

ET = ZoneInfo("America/New_York")


def load_universe(path: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    payload = json.loads(Path(path).read_text())
    symbols = tuple(f"US:{symbol}" for symbol in payload["symbols"])
    indicators = tuple(f"US:{symbol}" for symbol in payload["indicators_only"])
    return payload["version"], symbols, indicators


class TradingEngine:
    def __init__(
        self,
        settings: AppSettings,
        broker: BrokerPort,
        repository: RepositoryPort,
        strategies: tuple[StrategyPort, ...],
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.repository = repository
        self.strategies = strategies
        self.risk = RiskEngine(settings.risk)
        self.ledger = PortfolioLedger(settings.risk.starting_nav)
        self.orders = OrderRegistry()
        self.bars = BarStore()
        self.aggregator = MinuteBarAggregator()
        self.universe_version, self.symbols, self.indicators = load_universe(settings.universe_path)
        self.state = SystemState(status=SystemStatus.DISARMED, updated_at=datetime.now(UTC))
        self._state_lock = asyncio.Lock()
        self._event_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_decision_at: datetime | None = None
        self._latest_data_by_instrument: dict[str, datetime] = {}
        self._decision_arrivals: dict[datetime, set[str]] = {}
        self._decision_ready: dict[datetime, asyncio.Event] = {}
        self._active_symbols: set[tuple[str, str]] = set()
        self._intents: dict[str, OrderIntent] = {}
        self._remaining_reservations: dict[str, Decimal] = {}
        self._seen_idempotency_keys: set[str] = set()
        self._audit: list[dict[str, object]] = []
        self._decision_tasks: set[asyncio.Task[None]] = set()
        self._position_opened_at: dict[tuple[str, str], datetime] = {}
        self._position_entry_orders: dict[tuple[str, str], str] = {}
        self._closing_positions: set[tuple[str, str]] = set()
        self._rejections: deque[datetime] = deque()
        self._rejected_orders: set[str] = set()
        self._reconciliation_mismatches = 0

    async def start(self) -> None:
        if self.settings.mode is TradingMode.LIVE:
            raise RuntimeError("live mode is disabled")
        await self.broker.connect()
        await self.broker.verify_paper_account(self.settings.broker.account_id)
        await self.broker.subscribe(self.symbols + self.indicators)
        self._event_task = asyncio.create_task(self._consume_events(), name="broker-events")
        self._watchdog_task = asyncio.create_task(self._watchdog(), name="risk-watchdog")

    async def close(self) -> None:
        await self.disarm("system shutdown", actor="system")
        for task in self._decision_tasks:
            task.cancel()
        await asyncio.gather(*self._decision_tasks, return_exceptions=True)
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
        if self._event_task is not None:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
        await self.broker.disconnect()

    async def arm(self, actor: str = "operator") -> SystemState:
        async with self._state_lock:
            if self.state.status is SystemStatus.KILLED:
                raise RuntimeError("killed system must be acknowledged before arming")
            if self.broker.account_id != self.settings.broker.account_id:
                raise RuntimeError("paper account mismatch")
            broker_snapshot = await self.broker.account_snapshot()
            if broker_snapshot.positions:
                await self._set_state(SystemStatus.KILLED, "unexpected broker positions")
                raise RuntimeError("paper account must be flat before initial arming")
            now = datetime.now(UTC)
            stale = self._stale_instruments(now, self.indicators)
            if stale:
                raise RuntimeError(
                    "cannot arm without fresh regime market data: " + ", ".join(stale)
                )
            await self._set_state(SystemStatus.ARMED, "operator armed")
            await self._record_command("ARM", actor, "daily supervised arming")
            return self.state

    async def disarm(self, reason: str, actor: str = "operator") -> SystemState:
        async with self._state_lock:
            if self.state.status is not SystemStatus.KILLED:
                await self._set_state(SystemStatus.DISARMED, reason)
            await self._record_command("DISARM", actor, reason)
            return self.state

    async def kill(self, reason: str, actor: str = "operator") -> SystemState:
        async with self._state_lock:
            if self.state.status is SystemStatus.KILLED:
                return self.state
            await self._set_state(SystemStatus.KILLED, reason)
            await self.broker.cancel_all()
            await self._record_command("KILL", actor, reason)
            return self.state

    async def acknowledge_kill(self, reason: str, actor: str = "operator") -> SystemState:
        async with self._state_lock:
            if self.state.status is not SystemStatus.KILLED:
                raise RuntimeError("system is not killed")
            broker_snapshot = await self.broker.account_snapshot()
            if broker_snapshot.positions:
                raise RuntimeError("cannot acknowledge while broker has positions")
            await self._set_state(SystemStatus.DISARMED, reason)
            await self._record_command("ACKNOWLEDGE", actor, reason)
            return self.state

    async def cancel_all(self, actor: str = "operator") -> None:
        await self.broker.cancel_all()
        await self._record_command("CANCEL_ALL", actor, "operator request")

    async def submit_intent(self, intent: OrderIntent) -> BrokerOrder | None:
        if intent.idempotency_key in self._seen_idempotency_keys:
            await self._append_audit(
                "DUPLICATE_INTENT",
                "engine",
                {"idempotency_key": intent.idempotency_key},
            )
            return None
        self._seen_idempotency_keys.add(intent.idempotency_key)
        await self.repository.save_intent(intent)
        snapshot = self.ledger.snapshot()
        decision = self.risk.evaluate(
            intent,
            snapshot,
            datetime.now(UTC),
            self._oldest_data_at((*self.indicators, intent.instrument_id)),
            self.state.status is SystemStatus.ARMED,
            self._pending_entries(),
        )
        await self.repository.save_risk_decision(decision)
        if not decision.approved:
            await self._append_audit(
                "RISK_REJECTED",
                "risk",
                {
                    "intent_id": str(intent.intent_id),
                    "reasons": decision.reason_codes,
                },
            )
            return None
        reservation = intent.limit_price * Decimal(intent.quantity)
        if intent.effect is PositionEffect.OPEN:
            self.ledger.reserve(reservation)
        try:
            order = await self.broker.submit(intent)
        except Exception:
            if intent.effect is PositionEffect.OPEN:
                self.ledger.release(reservation)
            await self.kill("broker submission uncertainty", actor="engine")
            raise
        if not self.orders.add(intent.idempotency_key, order):
            if intent.effect is PositionEffect.OPEN:
                self.ledger.release(reservation)
            await self.kill("duplicate order intent", actor="engine")
            return None
        self._intents[str(intent.intent_id)] = intent
        if intent.effect is PositionEffect.OPEN:
            self._remaining_reservations[str(intent.intent_id)] = reservation
        self._active_symbols.add((intent.strategy, intent.instrument_id))
        try:
            await self.repository.save_order(order)
        except Exception:
            await self.kill("post-submission persistence failure", actor="engine")
            raise
        return order

    async def _consume_events(self) -> None:
        async for event in self.broker.events():
            try:
                if isinstance(event, BrokerFault):
                    await self._append_audit(
                        "BROKER_FAULT",
                        "broker",
                        event.model_dump(mode="json"),
                    )
                    if event.fatal:
                        await self.kill(
                            f"broker fault {event.code}: {event.message}", actor="engine"
                        )
                elif isinstance(event, MarketEvent):
                    await self._market_event(event)
                elif isinstance(event, Execution):
                    if not self.ledger.apply_execution(event):
                        await self._append_audit(
                            "DUPLICATE_EXECUTION",
                            "engine",
                            {"broker_execution_id": event.broker_execution_id},
                        )
                        continue
                    await self.repository.save_execution(event)
                    order = self.orders.get(str(event.internal_order_id))
                    intent = self._intents[str(order.intent_id)]
                    position_key = (event.strategy, event.instrument_id)
                    reservation_key = str(intent.intent_id)
                    if event.side is Side.BUY and reservation_key in self._remaining_reservations:
                        releasable = min(
                            self._remaining_reservations[reservation_key],
                            intent.limit_price * Decimal(event.quantity),
                        )
                        self.ledger.release(releasable)
                        self._remaining_reservations[reservation_key] -= releasable
                        self._position_opened_at.setdefault(position_key, event.executed_at)
                        self._position_entry_orders[position_key] = str(event.internal_order_id)
                    if (
                        event.side is Side.SELL
                        and self.ledger.quantity(event.strategy, event.instrument_id) == 0
                    ):
                        self._active_symbols.discard(position_key)
                        self._closing_positions.discard(position_key)
                        self._position_opened_at.pop(position_key, None)
                        self._position_entry_orders.pop(position_key, None)
                elif isinstance(event, BrokerOrder):
                    merged = self._merge_order_event(event)
                    await self.repository.save_order(merged)
                    if (
                        merged.state is OrderState.REJECTED
                        and str(merged.internal_order_id) not in self._rejected_orders
                    ):
                        self._rejected_orders.add(str(merged.internal_order_id))
                        if self._record_rejection(merged.updated_at):
                            await self.kill("broker rejection-rate threshold", actor="engine")
                    if merged.state in {
                        OrderState.CANCELLED,
                        OrderState.REJECTED,
                        OrderState.EXPIRED,
                    }:
                        key = str(event.intent_id)
                        remainder = self._remaining_reservations.pop(key, Decimal("0"))
                        if remainder:
                            self.ledger.release(remainder)
                        stored_intent = self._intents.get(key)
                        if (
                            stored_intent
                            and self.ledger.quantity(
                                stored_intent.strategy, stored_intent.instrument_id
                            )
                            == 0
                        ):
                            self._active_symbols.discard(
                                (stored_intent.strategy, stored_intent.instrument_id)
                            )
            except Exception as exc:
                await self.kill(f"event-processing failure: {exc}", actor="engine")

    async def _market_event(self, event: MarketEvent) -> None:
        previous = self._latest_data_by_instrument.get(event.instrument_id)
        if previous is None or event.received_at > previous:
            self._latest_data_by_instrument[event.instrument_id] = event.received_at
        await self.repository.save_event(event)
        bar = self.aggregator.update(event)
        if bar is None:
            return
        self.bars.append(bar)
        self.ledger.mark(bar.instrument_id, bar.close)
        await self.repository.save_bar(bar)
        self._register_decision_bar(bar)

    def _register_decision_bar(self, bar: Bar) -> None:
        if self.state.status is not SystemStatus.ARMED:
            return
        if bar.ended_at.minute % self.settings.strategy.decision_interval_minutes != 0:
            return
        arrivals = self._decision_arrivals.get(bar.ended_at)
        if self._last_decision_at is not None and bar.ended_at <= self._last_decision_at:
            if bar.ended_at == self._last_decision_at and arrivals is not None:
                arrivals.add(bar.instrument_id)
                if self._required_subscriptions().issubset(arrivals):
                    self._decision_ready[bar.ended_at].set()
            return

        self._last_decision_at = bar.ended_at
        arrivals = {bar.instrument_id}
        self._decision_arrivals[bar.ended_at] = arrivals
        ready = asyncio.Event()
        self._decision_ready[bar.ended_at] = ready
        if self._required_subscriptions().issubset(arrivals):
            ready.set()
        task = asyncio.create_task(
            self._guarded_decision_cycle(bar.ended_at), name=f"decision-{bar.ended_at}"
        )
        self._decision_tasks.add(task)
        task.add_done_callback(self._decision_tasks.discard)

    async def _guarded_decision_cycle(self, decision_at: datetime) -> None:
        try:
            ready = self._decision_ready[decision_at]
            try:
                await asyncio.wait_for(
                    ready.wait(),
                    timeout=self.settings.strategy.decision_collection_timeout_seconds,
                )
            except TimeoutError:
                if self.state.status is not SystemStatus.ARMED:
                    return
                arrivals = self._decision_arrivals.get(decision_at, set())
                missing = sorted(self._required_subscriptions().difference(arrivals))
                await self._append_audit(
                    "DECISION_COLLECTION_TIMEOUT",
                    "engine",
                    {
                        "decision_at": decision_at.isoformat(),
                        "arrived": len(arrivals),
                        "required": len(self._required_subscriptions()),
                        "missing": missing,
                    },
                )
            await self._decision_cycle(decision_at)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.kill(f"decision-cycle failure: {exc}", actor="engine")
        finally:
            self._decision_arrivals.pop(decision_at, None)
            self._decision_ready.pop(decision_at, None)

    async def _decision_cycle(self, decision_at: datetime) -> None:
        if self.state.status is not SystemStatus.ARMED:
            return
        local_time = decision_at.astimezone(ET).strftime("%H:%M")
        if local_time >= self.settings.strategy.flat_et:
            if self.ledger.snapshot().positions:
                await self.kill("positions remained at mandatory flat time", actor="engine")
            else:
                await self.disarm("session complete", actor="engine")
            return
        await self._exit_due_positions(
            decision_at,
            force_all=local_time >= self.settings.strategy.timed_exit_et,
        )
        if local_time >= self.settings.strategy.timed_exit_et:
            return
        aligned_spy = self._bar_for_decision("US:SPY", decision_at)
        aligned_qqq = self._bar_for_decision("US:QQQ", decision_at)
        if aligned_spy is None or aligned_qqq is None:
            await self._append_audit(
                "DECISION_SKIPPED_INCOMPLETE_REGIME",
                "engine",
                {
                    "decision_at": decision_at.isoformat(),
                    "spy_aligned": aligned_spy is not None,
                    "qqq_aligned": aligned_qqq is not None,
                },
            )
            return
        spy = self.bars.get("US:SPY")
        qqq = self.bars.get("US:QQQ")
        for strategy in self.strategies:
            if not self._strategy_window(strategy.name, local_time):
                continue
            for instrument_id in self.symbols:
                if (strategy.name, instrument_id) in self._active_symbols:
                    continue
                aligned_instrument = self._bar_for_decision(instrument_id, decision_at)
                if aligned_instrument is None:
                    continue
                feature_decision_at = max(
                    datetime.now(UTC),
                    decision_at,
                    aligned_instrument.available_at,
                    aligned_spy.available_at,
                    aligned_qqq.available_at,
                )
                features = build_features(
                    strategy.name,
                    instrument_id,
                    self.bars.get(instrument_id),
                    spy,
                    qqq,
                    feature_decision_at,
                )
                if features is None:
                    continue
                proposal = strategy.propose(features)
                if proposal is None or proposal.expected_return_bps <= proposal.estimated_cost_bps:
                    continue
                intent = self._intent_from_signal(proposal, features.values["last_price"])
                if intent is not None:
                    await self.submit_intent(intent)

    async def _exit_due_positions(self, decision_at: datetime, *, force_all: bool) -> None:
        maximum_age = timedelta(minutes=self.settings.strategy.maximum_holding_minutes)
        for position in self.ledger.snapshot(decision_at).positions:
            key = (position.strategy, position.instrument_id)
            opened_at = self._position_opened_at.get(key)
            if key in self._closing_positions or opened_at is None:
                continue
            if not force_all and decision_at - opened_at < maximum_age:
                continue
            bars = self.bars.get(position.instrument_id, 1)
            entry_order_id = self._position_entry_orders.get(key)
            if not bars or entry_order_id is None:
                await self.kill("cannot construct controlled time exit", actor="engine")
                return
            if bars[-1].ended_at != decision_at:
                await self.kill(
                    f"missing synchronized exit bar for {position.instrument_id}",
                    actor="engine",
                )
                return
            try:
                await self.broker.cancel(entry_order_id)
            except Exception:
                await self.kill("bracket cancellation uncertainty", actor="engine")
                return
            reference = bars[-1].close
            limit_price = (reference * Decimal("0.9995")).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            intent = OrderIntent(
                idempotency_key=(
                    f"time-exit:{position.strategy}:{position.instrument_id}:"
                    f"{decision_at.isoformat()}"
                ),
                strategy=position.strategy,
                instrument_id=position.instrument_id,
                side=Side.SELL,
                effect=PositionEffect.CLOSE,
                quantity=position.quantity,
                reference_price=reference,
                limit_price=limit_price,
                created_at=decision_at,
                expires_at=decision_at + timedelta(seconds=30),
            )
            order = await self.submit_intent(intent)
            if order is not None:
                self._closing_positions.add(key)

    def _required_subscriptions(self) -> set[str]:
        return set((*self.symbols, *self.indicators))

    def _bar_for_decision(self, instrument_id: str, decision_at: datetime) -> Bar | None:
        bars = self.bars.get(instrument_id, 1)
        if not bars or bars[-1].ended_at != decision_at:
            return None
        return bars[-1]

    def _strategy_window(self, strategy: str, local_time: str) -> bool:
        if strategy == "momentum":
            return (
                self.settings.strategy.momentum_start_et
                <= local_time
                <= self.settings.strategy.momentum_last_entry_et
            )
        return (
            self.settings.strategy.mean_reversion_start_et
            <= local_time
            <= self.settings.strategy.mean_reversion_last_entry_et
        )

    def _intent_from_signal(
        self, proposal: SignalProposal, last_price: float
    ) -> OrderIntent | None:
        price = Decimal(str(last_price)).quantize(Decimal("0.01"))
        stop = (price * Decimal("0.9925")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        risk = price - stop
        target = (price + risk * Decimal("2")).quantize(Decimal("0.01"))
        quantity = size_whole_shares(self.ledger.snapshot().nav, price, stop, self.settings.risk)
        if quantity < 1:
            return None
        limit_price = (price * Decimal("1.0005")).quantize(Decimal("0.01"))
        created_at = proposal.created_at
        return OrderIntent(
            idempotency_key=f"{proposal.signal_id}:entry",
            signal_id=proposal.signal_id,
            strategy=proposal.strategy,
            instrument_id=proposal.instrument_id,
            side=Side.BUY,
            effect=PositionEffect.OPEN,
            quantity=quantity,
            reference_price=price,
            limit_price=limit_price,
            stop_price=stop,
            target_price=target,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=10),
        )

    async def _watchdog(self) -> None:
        loop = asyncio.get_running_loop()
        last_reconciliation = loop.time()
        while True:
            await asyncio.sleep(1)
            if self.state.status is not SystemStatus.ARMED:
                continue
            now = datetime.now(UTC)
            snapshot = self.ledger.snapshot(now)
            required = set(self.indicators)
            required.update(position.instrument_id for position in snapshot.positions)
            required.update(instrument_id for _strategy, instrument_id in self._active_symbols)
            stale = self._stale_instruments(now, required)
            if stale:
                await self.kill(
                    "market data watchdog detected stale instruments: " + ", ".join(stale),
                    actor="engine",
                )
                continue
            daily_pnl = snapshot.daily_realized_pnl + snapshot.daily_unrealized_pnl
            if daily_pnl <= -(snapshot.nav * self.settings.risk.daily_loss_fraction):
                await self.kill("daily loss limit reached", actor="engine")
                continue
            if loop.time() - last_reconciliation < 30:
                continue
            last_reconciliation = loop.time()
            try:
                broker_snapshot = await self.broker.account_snapshot()
            except Exception:
                await self.kill("broker reconciliation unavailable", actor="engine")
                continue
            local_quantities: dict[str, int] = {}
            for position in snapshot.positions:
                local_quantities[position.instrument_id] = (
                    local_quantities.get(position.instrument_id, 0) + position.quantity
                )
            broker_quantities = {
                position.instrument_id: position.quantity
                for position in broker_snapshot.positions
                if position.quantity
            }
            if local_quantities == broker_quantities:
                self._reconciliation_mismatches = 0
            else:
                self._reconciliation_mismatches += 1
                await self._append_audit(
                    "RECONCILIATION_MISMATCH",
                    "engine",
                    {
                        "local": local_quantities,
                        "broker": broker_quantities,
                        "consecutive": self._reconciliation_mismatches,
                    },
                )
                if self._reconciliation_mismatches >= 2:
                    await self.kill("persistent broker position mismatch", actor="engine")

    def _record_rejection(self, rejected_at: datetime) -> bool:
        self._rejections.append(rejected_at)
        cutoff = rejected_at - timedelta(seconds=self.settings.risk.rejection_window_seconds)
        while self._rejections and self._rejections[0] < cutoff:
            self._rejections.popleft()
        return len(self._rejections) >= self.settings.risk.rejection_kill_count

    async def _set_state(self, status: SystemStatus, reason: str) -> None:
        now = datetime.now(UTC)
        self.state = SystemState(
            status=status,
            trading_day=now.astimezone(ET).date().isoformat(),
            paper_account_id=self.settings.broker.account_id,
            reason=reason,
            updated_at=now,
        )
        await self._append_audit("SYSTEM_STATE", "engine", self.state.model_dump(mode="json"))

    async def _record_command(self, command: str, actor: str, reason: str) -> None:
        value = OperatorCommand(
            command=command,
            actor=actor,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        await self._append_audit("OPERATOR_COMMAND", actor, value.model_dump(mode="json"))

    async def _append_audit(self, event_type: str, actor: str, payload: dict[str, object]) -> None:
        record: dict[str, object] = {
            "event_at": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        }
        self._audit.append(record)
        audit_method = getattr(self.repository, "audit", None)
        if audit_method is not None:
            await audit_method(datetime.now(UTC), event_type, actor, payload)

    def audit_log(self) -> tuple[dict[str, object], ...]:
        return tuple(self._audit[-500:])

    def _pending_entries(self) -> tuple[OrderIntent, ...]:
        pending: list[OrderIntent] = []
        for intent_id, reservation in self._remaining_reservations.items():
            intent = self._intents.get(intent_id)
            if intent is None or reservation <= 0:
                continue
            remaining_quantity = min(
                intent.quantity,
                max(1, int(reservation / intent.limit_price)),
            )
            pending.append(intent.model_copy(update={"quantity": remaining_quantity}))
        return tuple(pending)

    def _oldest_data_at(self, instrument_ids: tuple[str, ...]) -> datetime | None:
        timestamps = [
            self._latest_data_by_instrument[instrument_id]
            for instrument_id in set(instrument_ids)
            if instrument_id in self._latest_data_by_instrument
        ]
        if len(timestamps) != len(set(instrument_ids)):
            return None
        return min(timestamps)

    def _stale_instruments(
        self, now: datetime, instrument_ids: set[str] | tuple[str, ...]
    ) -> tuple[str, ...]:
        maximum_age = self.settings.risk.data_stale_seconds
        return tuple(
            sorted(
                instrument_id
                for instrument_id in set(instrument_ids)
                if instrument_id not in self._latest_data_by_instrument
                or (now - self._latest_data_by_instrument[instrument_id]).total_seconds()
                > maximum_age
            )
        )

    @property
    def latest_data_at(self) -> datetime | None:
        return max(self._latest_data_by_instrument.values(), default=None)

    def _merge_order_event(self, incoming: BrokerOrder) -> BrokerOrder:
        try:
            existing = self.orders.get(str(incoming.internal_order_id))
        except KeyError:
            return incoming
        if existing.state is incoming.state:
            merged = existing.model_copy(
                update={
                    "broker_order_id": incoming.broker_order_id or existing.broker_order_id,
                    "filled_quantity": max(existing.filled_quantity, incoming.filled_quantity),
                    "average_fill_price": (
                        incoming.average_fill_price or existing.average_fill_price
                    ),
                    "updated_at": max(existing.updated_at, incoming.updated_at),
                }
            )
        else:
            merged = transition_order(
                existing,
                incoming.state,
                incoming.updated_at,
                broker_order_id=incoming.broker_order_id,
                filled_quantity=incoming.filled_quantity,
                average_fill_price=incoming.average_fill_price,
            )
        self.orders.update(merged)
        return merged
