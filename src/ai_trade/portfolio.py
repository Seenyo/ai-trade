from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .domain import Execution, PortfolioSnapshot, PositionLot, Side


@dataclass
class _MutableLot:
    quantity: int
    average_price: Decimal
    market_price: Decimal


class PortfolioLedger:
    """Sleeve-aware, long-only cash ledger used by all execution modes."""

    def __init__(self, starting_cash: Decimal) -> None:
        self._cash = starting_cash
        self._settled_cash = starting_cash
        self._reserved_cash = Decimal("0")
        self._daily_realized_pnl = Decimal("0")
        self._positions: dict[tuple[str, str], _MutableLot] = {}
        self._seen_executions: set[str] = set()

    @property
    def available_settled_cash(self) -> Decimal:
        return self._settled_cash - self._reserved_cash

    def reserve(self, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("reservation must be positive")
        if amount > self.available_settled_cash:
            raise ValueError("insufficient settled cash")
        self._reserved_cash += amount

    def release(self, amount: Decimal) -> None:
        if amount < 0 or amount > self._reserved_cash:
            raise ValueError("invalid reservation release")
        self._reserved_cash -= amount

    def quantity(self, strategy: str, instrument_id: str) -> int:
        lot = self._positions.get((strategy, instrument_id))
        return lot.quantity if lot else 0

    def apply_execution(self, execution: Execution) -> bool:
        """Apply once; return False when the broker repeats an execution callback."""
        if execution.broker_execution_id in self._seen_executions:
            return False
        self._seen_executions.add(execution.broker_execution_id)
        key = (execution.strategy, execution.instrument_id)
        lot = self._positions.get(key)
        consideration = execution.price * Decimal(execution.quantity)

        if execution.side is Side.BUY:
            old_quantity = lot.quantity if lot else 0
            old_cost = (lot.average_price * Decimal(old_quantity)) if lot else Decimal("0")
            new_quantity = old_quantity + execution.quantity
            self._positions[key] = _MutableLot(
                quantity=new_quantity,
                average_price=(old_cost + consideration) / Decimal(new_quantity),
                market_price=execution.price,
            )
            self._cash -= consideration + execution.commission
            self._settled_cash -= consideration + execution.commission
        else:
            if lot is None or execution.quantity > lot.quantity:
                raise ValueError("sell execution exceeds sleeve-owned quantity")
            realized = (execution.price - lot.average_price) * Decimal(execution.quantity)
            realized -= execution.commission
            self._daily_realized_pnl += realized
            lot.quantity -= execution.quantity
            lot.market_price = execution.price
            self._cash += consideration - execution.commission
            # Sale proceeds remain unsettled until an explicit settlement event.
            if lot.quantity == 0:
                del self._positions[key]
        return True

    def settle_cash(self) -> None:
        self._settled_cash = self._cash

    def mark(self, instrument_id: str, price: Decimal) -> None:
        for (_strategy, symbol), lot in self._positions.items():
            if symbol == instrument_id:
                lot.market_price = price

    def snapshot(self, captured_at: datetime | None = None) -> PortfolioSnapshot:
        positions = tuple(
            PositionLot(
                strategy=strategy,
                instrument_id=instrument_id,
                quantity=lot.quantity,
                average_price=lot.average_price,
                market_price=lot.market_price,
            )
            for (strategy, instrument_id), lot in sorted(self._positions.items())
        )
        market_value = sum((position.market_value for position in positions), Decimal("0"))
        cost_value = sum(
            (position.average_price * Decimal(position.quantity) for position in positions),
            Decimal("0"),
        )
        unrealized = market_value - cost_value
        return PortfolioSnapshot(
            captured_at=captured_at or datetime.now(UTC),
            nav=self._cash + market_value,
            cash=self._cash,
            settled_cash=self._settled_cash,
            reserved_cash=self._reserved_cash,
            daily_realized_pnl=self._daily_realized_pnl,
            daily_unrealized_pnl=unrealized,
            positions=positions,
        )
