from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from joblib import load

from ..domain import FeatureSnapshot, Side, SignalProposal


class ProbabilityModel(Protocol):
    def predict_proba(self, values: list[list[float]]) -> Any: ...


class ModelScorer:
    def __init__(self, artifact_path: str | None, feature_names: tuple[str, ...]) -> None:
        self.feature_names = feature_names
        self.model: ProbabilityModel | None = None
        if artifact_path and Path(artifact_path).exists():
            self.model = load(artifact_path)

    def probability(self, snapshot: FeatureSnapshot, baseline: float) -> float:
        if self.model is None:
            return baseline
        row = [[snapshot.values[name] for name in self.feature_names]]
        probabilities = self.model.predict_proba(row)
        return float(probabilities[0][1])


def signal(
    snapshot: FeatureSnapshot,
    strategy: str,
    model_version: str,
    probability: float,
    expected_return_bps: Decimal,
    estimated_cost_bps: Decimal,
) -> SignalProposal:
    return SignalProposal(
        strategy=strategy,
        model_version=model_version,
        instrument_id=snapshot.instrument_id,
        side=Side.BUY,
        confidence=probability,
        expected_return_bps=expected_return_bps,
        estimated_cost_bps=estimated_cost_bps,
        horizon_seconds=1800,
        created_at=snapshot.decision_at,
        expires_at=snapshot.decision_at + timedelta(minutes=5),
        feature_snapshot_id=snapshot.snapshot_id,
    )
