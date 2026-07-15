from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import sqrt
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl

from .config import AppSettings
from .domain import Bar, Execution, OrderIntent, PositionEffect, Side
from .market import build_features
from .portfolio import PortfolioLedger
from .ports import StrategyPort
from .risk import RiskEngine, size_whole_shares

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BacktestTrade:
    strategy: str
    instrument_id: str
    entered_at: datetime
    exited_at: datetime
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    net_pnl: Decimal
    exit_reason: str


@dataclass(frozen=True)
class BacktestResult:
    starting_nav: Decimal
    ending_nav: Decimal
    trades: tuple[BacktestTrade, ...]
    maximum_drawdown_fraction: float
    annualized_sharpe: float


@dataclass
class _OpenTrade:
    strategy: str
    instrument_id: str
    quantity: int
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    entered_at: datetime
    expires_at: datetime


class BacktestRunner:
    """Conservative next-bar fill replay using production feature and risk contracts."""

    def __init__(self, settings: AppSettings, strategies: Sequence[StrategyPort]) -> None:
        self.settings = settings
        self.strategies = strategies
        self.risk = RiskEngine(settings.risk)
        self.ledger = PortfolioLedger(settings.risk.starting_nav)
        self._open: dict[tuple[str, str], _OpenTrade] = {}
        self._pending: list[OrderIntent] = []
        self._trades: list[BacktestTrade] = []
        self._execution_counter = 0

    def run(self, bars: Iterable[Bar], symbols: Sequence[str]) -> BacktestResult:
        by_symbol: dict[str, list[Bar]] = defaultdict(list)
        by_time: dict[datetime, dict[str, Bar]] = defaultdict(dict)
        for bar in sorted(bars, key=lambda item: (item.ended_at, item.instrument_id)):
            by_symbol[bar.instrument_id].append(bar)
            by_time[bar.ended_at][bar.instrument_id] = bar

        equity_curve: list[Decimal] = [self.settings.risk.starting_nav]
        for decision_at in sorted(by_time):
            current = by_time[decision_at]
            self._fill_pending(current, decision_at)
            self._evaluate_exits(current, decision_at)
            for instrument_id, bar in current.items():
                self.ledger.mark(instrument_id, bar.close)
            local_time = decision_at.astimezone(ET).strftime("%H:%M")
            if local_time >= self.settings.strategy.timed_exit_et:
                self._cancel_pending()
                self._force_close(current, decision_at, "daily_flatten")
            elif decision_at.minute % self.settings.strategy.decision_interval_minutes == 0:
                self._generate_signals(by_symbol, current, symbols, decision_at)
            equity_curve.append(self.ledger.snapshot(decision_at).nav)

        final_time = max(by_time) if by_time else datetime.min
        if by_time:
            self._cancel_pending()
            self._force_close(by_time[final_time], final_time, "dataset_end")
        ending_nav = (
            self.ledger.snapshot(final_time).nav if by_time else self.settings.risk.starting_nav
        )
        return BacktestResult(
            starting_nav=self.settings.risk.starting_nav,
            ending_nav=ending_nav,
            trades=tuple(self._trades),
            maximum_drawdown_fraction=_maximum_drawdown(equity_curve),
            annualized_sharpe=_sharpe(equity_curve),
        )

    def _generate_signals(
        self,
        histories: dict[str, list[Bar]],
        current: dict[str, Bar],
        symbols: Sequence[str],
        decision_at: datetime,
    ) -> None:
        spy = [item for item in histories.get("US:SPY", []) if item.ended_at <= decision_at]
        qqq = [item for item in histories.get("US:QQQ", []) if item.ended_at <= decision_at]
        for strategy in self.strategies:
            local_time = decision_at.astimezone(ET).strftime("%H:%M")
            if not self._strategy_window(strategy.name, local_time):
                continue
            for instrument_id in symbols:
                if (strategy.name, instrument_id) in self._open or any(
                    pending.strategy == strategy.name and pending.instrument_id == instrument_id
                    for pending in self._pending
                ):
                    continue
                bar = current.get(instrument_id)
                if bar is None:
                    continue
                history = [
                    item
                    for item in histories.get(instrument_id, [])
                    if item.ended_at <= decision_at
                ]
                features = build_features(
                    strategy.name, instrument_id, history, spy, qqq, decision_at
                )
                if features is None:
                    continue
                proposal = strategy.propose(features)
                if proposal is None or proposal.expected_return_bps <= proposal.estimated_cost_bps:
                    continue
                price = bar.close
                stop = (price * Decimal("0.9925")).quantize(Decimal("0.01"))
                quantity = size_whole_shares(
                    self.ledger.snapshot(decision_at).nav, price, stop, self.settings.risk
                )
                if quantity == 0:
                    continue
                risk = price - stop
                intent = OrderIntent(
                    idempotency_key=f"backtest:{proposal.signal_id}",
                    signal_id=proposal.signal_id,
                    strategy=strategy.name,
                    instrument_id=instrument_id,
                    side=Side.BUY,
                    effect=PositionEffect.OPEN,
                    quantity=quantity,
                    reference_price=price,
                    limit_price=price * Decimal("1.0005"),
                    stop_price=stop,
                    target_price=price + risk * Decimal("2"),
                    created_at=decision_at,
                    expires_at=decision_at + timedelta(minutes=5),
                )
                decision = self.risk.evaluate(
                    intent,
                    self.ledger.snapshot(decision_at),
                    decision_at,
                    decision_at,
                    True,
                    self._pending,
                )
                if decision.approved:
                    self.ledger.reserve(intent.limit_price * Decimal(intent.quantity))
                    self._pending.append(intent)

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

    def _fill_pending(self, current: dict[str, Bar], now: datetime) -> None:
        remaining: list[OrderIntent] = []
        for intent in self._pending:
            bar = current.get(intent.instrument_id)
            if now > intent.expires_at:
                self.ledger.release(intent.limit_price * Decimal(intent.quantity))
                continue
            if bar is None or now <= intent.created_at:
                remaining.append(intent)
                continue
            if bar.low > intent.limit_price:
                remaining.append(intent)
                continue
            capacity = max(1, int(bar.volume * Decimal("0.05")))
            quantity = min(intent.quantity, capacity)
            raw_price = max(bar.open, intent.reference_price)
            price = min(intent.limit_price, raw_price * Decimal("1.0003"))
            commission = max(Decimal("1"), Decimal(quantity) * Decimal("0.005"))
            self.ledger.release(intent.limit_price * Decimal(intent.quantity))
            execution = self._execution(intent, quantity, price, commission, now)
            self.ledger.apply_execution(execution)
            self._open[(intent.strategy, intent.instrument_id)] = _OpenTrade(
                strategy=intent.strategy,
                instrument_id=intent.instrument_id,
                quantity=quantity,
                entry_price=price,
                stop_price=intent.stop_price or price * Decimal("0.99"),
                target_price=intent.target_price or price * Decimal("1.02"),
                entered_at=now,
                expires_at=now + timedelta(minutes=self.settings.strategy.maximum_holding_minutes),
            )
        self._pending = remaining

    def _evaluate_exits(self, current: dict[str, Bar], now: datetime) -> None:
        for key, trade in list(self._open.items()):
            bar = current.get(trade.instrument_id)
            if bar is None:
                continue
            reason = None
            raw_price = bar.close
            # When stop and target both occur in one bar, assume the adverse event occurred first.
            if bar.low <= trade.stop_price:
                reason = "stop"
                raw_price = min(bar.open, trade.stop_price)
            elif bar.high >= trade.target_price:
                reason = "target"
                raw_price = max(bar.open, trade.target_price)
            elif now >= trade.expires_at:
                reason = "time"
            if reason is not None:
                self._close(key, trade, raw_price * Decimal("0.9997"), now, reason)

    def _cancel_pending(self) -> None:
        for intent in self._pending:
            self.ledger.release(intent.limit_price * Decimal(intent.quantity))
        self._pending.clear()

    def _force_close(self, current: dict[str, Bar], now: datetime, reason: str) -> None:
        for key, trade in list(self._open.items()):
            bar = current.get(trade.instrument_id)
            if bar is not None:
                self._close(key, trade, bar.close * Decimal("0.9997"), now, reason)

    def _close(
        self,
        key: tuple[str, str],
        trade: _OpenTrade,
        price: Decimal,
        now: datetime,
        reason: str,
    ) -> None:
        commission = max(Decimal("1"), Decimal(trade.quantity) * Decimal("0.005"))
        intent = OrderIntent(
            idempotency_key=f"backtest-exit:{uuid4()}",
            strategy=trade.strategy,
            instrument_id=trade.instrument_id,
            side=Side.SELL,
            effect=PositionEffect.CLOSE,
            quantity=trade.quantity,
            reference_price=price,
            limit_price=price,
            created_at=now,
            expires_at=now + timedelta(seconds=10),
        )
        execution = self._execution(intent, trade.quantity, price, commission, now)
        self.ledger.apply_execution(execution)
        net_pnl = (price - trade.entry_price) * Decimal(trade.quantity) - commission * Decimal("2")
        self._trades.append(
            BacktestTrade(
                strategy=trade.strategy,
                instrument_id=trade.instrument_id,
                entered_at=trade.entered_at,
                exited_at=now,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                exit_price=price,
                net_pnl=net_pnl,
                exit_reason=reason,
            )
        )
        del self._open[key]

    def _execution(
        self,
        intent: OrderIntent,
        quantity: int,
        price: Decimal,
        commission: Decimal,
        now: datetime,
    ) -> Execution:
        self._execution_counter += 1
        return Execution(
            broker_execution_id=f"backtest-{self._execution_counter}",
            internal_order_id=uuid4(),
            strategy=intent.strategy,
            instrument_id=intent.instrument_id,
            side=intent.side,
            quantity=quantity,
            price=price,
            commission=commission,
            executed_at=now,
        )


def _maximum_drawdown(values: Sequence[Decimal]) -> float:
    peak = values[0]
    maximum = Decimal("0")
    for value in values:
        peak = max(peak, value)
        if peak:
            maximum = max(maximum, (peak - value) / peak)
    return float(maximum)


def _sharpe(values: Sequence[Decimal]) -> float:
    if len(values) < 3:
        return 0.0
    returns = [
        float(values[index] / values[index - 1] - 1)
        for index in range(1, len(values))
        if values[index - 1]
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return mean / sqrt(variance) * sqrt(252 * 78) if variance > 0 else 0.0


def load_bars(path: Path) -> list[Bar]:
    required = {
        "instrument_id",
        "started_at",
        "ended_at",
        "available_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    frame = pl.read_parquet(path)
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"bar dataset is missing columns: {sorted(missing)}")
    bars: list[Bar] = []
    for row in frame.iter_rows(named=True):
        bars.append(
            Bar(
                instrument_id=str(row["instrument_id"]),
                interval_seconds=int(row.get("interval_seconds", 60)),
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                available_at=row["available_at"],
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                bid=Decimal(str(row["bid"])) if row.get("bid") is not None else None,
                ask=Decimal(str(row["ask"])) if row.get("ask") is not None else None,
                complete=bool(row.get("complete", True)),
            )
        )
    return bars


def main() -> None:
    from .config import TradingMode
    from .engine import load_universe
    from .strategies import MeanReversionStrategy, MomentumStrategy

    parser = argparse.ArgumentParser(description="Replay canonical minute bars")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--momentum-model")
    parser.add_argument("--mean-reversion-model")
    arguments = parser.parse_args()
    settings = AppSettings(mode=TradingMode.BACKTEST)
    _, symbols, _ = load_universe(settings.universe_path)
    runner = BacktestRunner(
        settings,
        (
            MomentumStrategy(arguments.momentum_model),
            MeanReversionStrategy(arguments.mean_reversion_model),
        ),
    )
    result = runner.run(load_bars(arguments.dataset), symbols)
    payload = {
        "starting_nav": str(result.starting_nav),
        "ending_nav": str(result.ending_nav),
        "maximum_drawdown_fraction": result.maximum_drawdown_fraction,
        "annualized_sharpe": result.annualized_sharpe,
        "trade_count": len(result.trades),
        "trades": [
            {
                **trade.__dict__,
                "entered_at": trade.entered_at.isoformat(),
                "exited_at": trade.exited_at.isoformat(),
                "entry_price": str(trade.entry_price),
                "exit_price": str(trade.exit_price),
                "net_pnl": str(trade.net_pnl),
            }
            for trade in result.trades
        ],
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
