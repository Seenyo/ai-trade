from __future__ import annotations

from datetime import date, timedelta

from ai_trade.qualification import PaperSessionMetric, evaluate_paper_gate


def test_paper_gate_requires_both_sleeves_and_minimum_evidence() -> None:
    start = date(2026, 1, 2)
    metrics = [
        PaperSessionMetric(
            session_date=start + timedelta(days=index),
            strategy=strategy,
            trade_count=3,
            net_return=0.001,
        )
        for index in range(90)
        for strategy in ("momentum", "mean_reversion")
    ]

    result = evaluate_paper_gate(metrics)

    assert result.qualified
    assert result.sessions == 90
    assert result.trades == 540


def test_paper_gate_fails_without_evidence() -> None:
    result = evaluate_paper_gate(())
    assert not result.qualified
    assert "SESSION_COUNT:0/90" in result.failures
