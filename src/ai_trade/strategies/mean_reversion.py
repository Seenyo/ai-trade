from __future__ import annotations

from decimal import Decimal

from ..domain import FeatureSnapshot, SignalProposal
from .base import ModelScorer, signal

FEATURES = (
    "return_5m",
    "return_15m",
    "vwap_distance",
    "range_position_30m",
    "volume_z",
    "realized_vol_30m",
    "spy_return_15m",
    "spread_bps",
)


class MeanReversionStrategy:
    name = "mean_reversion"

    def __init__(self, artifact_path: str | None = None) -> None:
        self.scorer = ModelScorer(artifact_path, FEATURES)

    def propose(self, snapshot: FeatureSnapshot) -> SignalProposal | None:
        value = snapshot.values
        market_not_trending_down = value["spy_return_15m"] > -0.004
        qualifies = (
            value["return_5m"] < -0.0025
            and value["vwap_distance"] < -0.004
            and value["range_position_30m"] < 0.25
            and value["volume_z"] > 0.0
            and value["spread_bps"] <= 8
            and market_not_trending_down
        )
        if not qualifies:
            return None
        baseline_probability = min(0.72, 0.56 + abs(value["vwap_distance"]) * 8)
        probability = self.scorer.probability(snapshot, baseline_probability)
        if probability < 0.58:
            return None
        expected = Decimal(str(abs(value["vwap_distance"]) * 10000 * 0.30))
        return signal(
            snapshot,
            self.name,
            "mean-reversion-baseline-v1",
            probability,
            expected,
            Decimal("8"),
        )
