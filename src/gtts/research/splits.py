"""Chronological walk-forward splits and train-fitted standardization."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite, sqrt


@dataclass(frozen=True)
class WalkForwardSplit:
    fold: int
    train: tuple[date, ...]
    validation: tuple[date, ...]
    oos: tuple[date, ...]
    purged: tuple[date, ...]
    embargoed: tuple[date, ...]


def walk_forward_splits(
    sessions: Sequence[date],
    *,
    train_size: int,
    validation_size: int,
    oos_size: int,
    purge_size: int = 0,
    embargo_size: int = 0,
) -> tuple[WalkForwardSplit, ...]:
    """Make fixed-size rolling windows; sizes and gaps are trading-session counts."""
    if any(type(size) is not int or size < 0 for size in (purge_size, embargo_size)):
        raise ValueError("purge_size and embargo_size must be non-negative integers")
    if any(type(size) is not int or size <= 0 for size in (train_size, validation_size, oos_size)):
        raise ValueError("train_size, validation_size, and oos_size must be positive integers")
    if tuple(sessions) != tuple(sorted(set(sessions))):
        raise ValueError("sessions must be unique and in chronological order")

    rows = tuple(sessions)
    splits: list[WalkForwardSplit] = []
    start = 0
    fold = 1
    while True:
        train_end = start + train_size
        validation_start = train_end + purge_size
        validation_end = validation_start + validation_size
        oos_start = validation_end + purge_size
        oos_end = oos_start + oos_size
        if oos_end > len(rows):
            break
        embargo_end = min(oos_end + embargo_size, len(rows))
        splits.append(
            WalkForwardSplit(
                fold=fold,
                train=rows[start:train_end],
                validation=rows[validation_start:validation_end],
                oos=rows[oos_start:oos_end],
                purged=rows[train_end:validation_start] + rows[validation_end:oos_start],
                embargoed=rows[oos_end:embargo_end],
            )
        )
        start = oos_end + embargo_size
        fold += 1
    return tuple(splits)


@dataclass(frozen=True)
class Standardizer:
    mean: tuple[float, ...]
    scale: tuple[float, ...]

    def transform(self, rows: Iterable[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
        transformed: list[tuple[float, ...]] = []
        for row in rows:
            if len(row) != len(self.mean):
                raise ValueError("feature width differs from fitted training data")
            values = tuple(float(value) for value in row)
            if not all(isfinite(value) for value in values):
                raise ValueError("features must be finite")
            transformed.append(
                tuple(
                    (value - mean) / scale
                    for value, mean, scale in zip(values, self.mean, self.scale)
                )
            )
        return tuple(transformed)


def fit_standardizer(training_rows: Iterable[Sequence[float]]) -> Standardizer:
    """Fit population means/scales on training rows only; reuse the result for later periods."""
    rows = tuple(tuple(float(value) for value in row) for row in training_rows)
    if not rows:
        raise ValueError("at least one training row is required")
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ValueError("training rows must have one consistent, non-empty feature width")
    if not all(isfinite(value) for row in rows for value in row):
        raise ValueError("training features must be finite")
    means = tuple(sum(row[column] for row in rows) / len(rows) for column in range(width))
    scales = tuple(
        sqrt(sum((row[column] - means[column]) ** 2 for row in rows) / len(rows)) or 1.0
        for column in range(width)
    )
    return Standardizer(means, scales)
