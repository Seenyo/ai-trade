from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score


@dataclass(frozen=True)
class Fold:
    train: np.ndarray
    test: np.ndarray


class PurgedWalkForwardSplit:
    def __init__(self, folds: int = 5, embargo_rows: int = 60) -> None:
        if folds < 2:
            raise ValueError("at least two folds are required")
        self.folds = folds
        self.embargo_rows = embargo_rows

    def split(self, rows: int) -> list[Fold]:
        test_size = rows // (self.folds + 1)
        if test_size <= self.embargo_rows:
            raise ValueError("not enough rows for requested purge/embargo")
        result: list[Fold] = []
        for index in range(1, self.folds + 1):
            test_start = index * test_size
            test_end = rows if index == self.folds else (index + 1) * test_size
            train_end = test_start - self.embargo_rows
            if train_end <= 0:
                continue
            result.append(Fold(np.arange(0, train_end), np.arange(test_start, test_end)))
        return result


def train(dataset: Path, output: Path, features: list[str], target: str) -> dict[str, float]:
    frame = pl.read_parquet(dataset).sort("available_at")
    clean = frame.drop_nulls([*features, target])
    matrix = clean.select(features).to_numpy()
    labels = clean[target].to_numpy()
    splitter = PurgedWalkForwardSplit()
    probabilities = np.full(len(clean), np.nan)
    for fold in splitter.split(len(clean)):
        model = LGBMClassifier(
            n_estimators=300,
            learning_rate=0.03,
            max_depth=5,
            num_leaves=24,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=17,
        )
        model.fit(matrix[fold.train], labels[fold.train])
        probabilities[fold.test] = model.predict_proba(matrix[fold.test])[:, 1]
    evaluated = ~np.isnan(probabilities)
    metrics = {
        "roc_auc": float(roc_auc_score(labels[evaluated], probabilities[evaluated])),
        "brier": float(brier_score_loss(labels[evaluated], probabilities[evaluated])),
        "evaluated_rows": float(evaluated.sum()),
    }
    final_model = LGBMClassifier(
        n_estimators=300,
        learning_rate=0.03,
        max_depth=5,
        num_leaves=24,
        random_state=17,
    )
    final_model.fit(matrix, labels)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, output)
    output.with_suffix(".metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target", default="net_positive_30m")
    parser.add_argument("--features", nargs="+", required=True)
    arguments = parser.parse_args()
    metrics = train(
        arguments.dataset,
        arguments.output,
        arguments.features,
        arguments.target,
    )
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
