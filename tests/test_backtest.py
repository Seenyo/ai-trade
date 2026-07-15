from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ai_trade.backtest import BacktestRunner
from ai_trade.config import AppSettings, TradingMode
from ai_trade.domain import FeatureSnapshot, Side, SignalProposal
from ai_trade.market import synthetic_bar


class _AlwaysBuy:
    name = "momentum"

    def propose(self, snapshot: FeatureSnapshot) -> SignalProposal:
        return SignalProposal(
            strategy=self.name,
            model_version="test",
            instrument_id=snapshot.instrument_id,
            side=Side.BUY,
            confidence=0.75,
            expected_return_bps=Decimal("20"),
            estimated_cost_bps=Decimal("5"),
            horizon_seconds=1800,
            created_at=snapshot.decision_at,
            expires_at=snapshot.decision_at + timedelta(minutes=5),
            feature_snapshot_id=snapshot.snapshot_id,
        )


def test_dataset_end_closes_with_latest_causal_instrument_bar(now) -> None:
    start = now - timedelta(minutes=35)
    bars = [
        synthetic_bar(
            instrument_id,
            start + timedelta(minutes=index),
            Decimal("100"),
            Decimal("10000"),
        )
        for index in range(35)
        for instrument_id in ("US:AAPL", "US:SPY", "US:QQQ")
    ]
    bars.extend(
        synthetic_bar(
            instrument_id,
            now,
            Decimal("100"),
            Decimal("10000"),
        )
        for instrument_id in ("US:AAPL", "US:SPY", "US:QQQ")
    )
    # The dataset's last bucket has regime bars but no AAPL bar.
    bars.extend(
        synthetic_bar(
            instrument_id,
            now + timedelta(minutes=1),
            Decimal("100"),
            Decimal("10000"),
        )
        for instrument_id in ("US:SPY", "US:QQQ")
    )

    result = BacktestRunner(
        AppSettings(mode=TradingMode.BACKTEST),
        (_AlwaysBuy(),),
    ).run(bars, ("US:AAPL",))

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "dataset_end"
    assert result.trades[0].exited_at == now + timedelta(minutes=2)
    assert result.trades[0].exit_price == Decimal("99.9700")
