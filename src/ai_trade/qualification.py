from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

import polars as pl


@dataclass(frozen=True)
class PaperSessionMetric:
    session_date: date
    strategy: str
    trade_count: int
    net_return: float


@dataclass(frozen=True)
class StrategyQualification:
    sessions: int
    trades: int
    annualized_sharpe: float
    lower_95_daily_mean: float
    maximum_drawdown_fraction: float
    qualified: bool


@dataclass(frozen=True)
class QualificationResult:
    sessions: int
    trades: int
    strategies: dict[str, StrategyQualification]
    qualified: bool
    failures: tuple[str, ...]


def evaluate_paper_gate(
    metrics: Iterable[PaperSessionMetric],
    *,
    expected_strategies: tuple[str, ...] = ("momentum", "mean_reversion"),
    minimum_sessions: int = 90,
    minimum_trades: int = 500,
) -> QualificationResult:
    values = tuple(metrics)
    sessions = len({item.session_date for item in values})
    trades = sum(item.trade_count for item in values)
    by_strategy: dict[str, list[PaperSessionMetric]] = defaultdict(list)
    for item in values:
        by_strategy[item.strategy].append(item)

    failures: list[str] = []
    if sessions < minimum_sessions:
        failures.append(f"SESSION_COUNT:{sessions}/{minimum_sessions}")
    if trades < minimum_trades:
        failures.append(f"TRADE_COUNT:{trades}/{minimum_trades}")

    strategy_results: dict[str, StrategyQualification] = {}
    minimum_strategy_trades = max(100, minimum_trades // 4)
    for strategy in expected_strategies:
        rows = by_strategy.get(strategy, [])
        daily: dict[date, float] = defaultdict(float)
        strategy_trades = 0
        for row in rows:
            daily[row.session_date] += row.net_return
            strategy_trades += row.trade_count
        returns = [daily[day] for day in sorted(daily)]
        mean = fmean(returns) if returns else 0.0
        deviation = stdev(returns) if len(returns) > 1 else 0.0
        lower_mean = mean - 1.96 * deviation / sqrt(len(returns)) if returns else 0.0
        sharpe = mean / deviation * sqrt(252) if deviation > 0 else 0.0
        drawdown = _maximum_drawdown(returns)
        strategy_ok = (
            len(returns) >= min(60, minimum_sessions)
            and strategy_trades >= minimum_strategy_trades
            and lower_mean > 0
            and drawdown <= 0.10
        )
        if not strategy_ok:
            failures.append(f"STRATEGY_QUALIFICATION:{strategy}")
        strategy_results[strategy] = StrategyQualification(
            sessions=len(returns),
            trades=strategy_trades,
            annualized_sharpe=sharpe,
            lower_95_daily_mean=lower_mean,
            maximum_drawdown_fraction=drawdown,
            qualified=strategy_ok,
        )
    return QualificationResult(
        sessions=sessions,
        trades=trades,
        strategies=strategy_results,
        qualified=not failures,
        failures=tuple(failures),
    )


def _maximum_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def load_session_metrics(path: Path) -> list[PaperSessionMetric]:
    frame = pl.read_parquet(path) if path.suffix == ".parquet" else pl.read_csv(path)
    required = {"session_date", "strategy", "trade_count", "net_return"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"paper summary is missing columns: {sorted(missing)}")
    return [
        PaperSessionMetric(
            session_date=date.fromisoformat(str(row["session_date"])),
            strategy=str(row["strategy"]),
            trade_count=int(row["trade_count"]),
            net_return=float(row["net_return"]),
        )
        for row in frame.iter_rows(named=True)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the paper-to-live evidence gate")
    parser.add_argument("summary", type=Path)
    arguments = parser.parse_args()
    result = evaluate_paper_gate(load_session_metrics(arguments.summary))
    print(json.dumps(asdict(result), indent=2))
    if not result.qualified:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
