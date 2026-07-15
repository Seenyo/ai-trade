from __future__ import annotations

from decimal import Decimal

from ..domain import FeatureSnapshot, SignalProposal
from .base import ModelScorer, signal

FEATURES = (
    "return_5m",
    "return_15m",
    "return_30m",
    "relative_spy_15m",
    "relative_qqq_15m",
    "volume_z",
    "realized_vol_30m",
    "range_position_30m",
    "spread_bps",
)


class MomentumStrategy:
    name = "momentum"

    def __init__(self, artifact_path: str | None = None) -> None:
        self.scorer = ModelScorer(artifact_path, FEATURES)

    def propose(self, snapshot: FeatureSnapshot) -> SignalProposal | None:
        value = snapshot.values
        qualifies = (
            value["return_15m"] > 0.002
            and value["relative_spy_15m"] > 0.001
            and value["volume_z"] > 0.5
            and value["range_position_30m"] > 0.85
            and value["spread_bps"] <= 8
        )
        if not qualifies:
            return None
        baseline_probability = min(0.75, 0.55 + value["volume_z"] * 0.03)
        probability = self.scorer.probability(snapshot, baseline_probability)
        if probability < 0.58:
            return None
        expected = Decimal(str(max(0.0, value["relative_spy_15m"] * 10000 * 0.35)))
        return signal(
            snapshot,
            self.name,
            "momentum-baseline-v1",
            probability,
            expected,
            Decimal("8"),
        )
