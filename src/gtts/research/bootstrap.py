"""Paired circular block bootstrap for dated strategy/baseline observations."""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from gtts.research import MINIMUM_BOOTSTRAP_RESAMPLES, MINIMUM_RESEARCH_OBSERVATIONS


@dataclass(frozen=True)
class BootstrapInterval:
    mean_difference: float
    lower: float
    upper: float
    observations: int
    block_size: int
    resamples: int
    seed: int
    significant: bool
    status: str


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if sorted_values[0] == sorted_values[-1]:
        return sorted_values[0]
    index = (len(sorted_values) - 1) * probability
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = index - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def paired_block_bootstrap(
    strategy: Sequence[float],
    baseline: Sequence[float],
    *,
    block_size: int = 5,
    resamples: int = 2_000,
    confidence: float = 0.95,
    seed: int = 20_261_116,
    minimum_observations: int = MINIMUM_RESEARCH_OBSERVATIONS,
) -> BootstrapInterval:
    """Resample paired differences in circular time blocks; never tune on OOS."""
    if len(strategy) != len(baseline) or not strategy:
        raise ValueError("strategy and baseline must have the same non-empty length")
    if type(block_size) is not int or block_size <= 0 or 2 * block_size > len(strategy):
        raise ValueError("block_size must be positive and no larger than half the sample")
    if type(resamples) is not int or resamples < MINIMUM_BOOTSTRAP_RESAMPLES:
        raise ValueError(f"resamples must be at least {MINIMUM_BOOTSTRAP_RESAMPLES}")
    if (
        type(minimum_observations) is not int
        or minimum_observations < MINIMUM_RESEARCH_OBSERVATIONS
    ):
        raise ValueError(f"minimum_observations must be at least {MINIMUM_RESEARCH_OBSERVATIONS}")
    if not 0 < confidence < 1 or type(seed) is not int:
        raise ValueError("confidence must be between zero and one and seed must be an integer")
    if not all(isfinite(value) for value in (*strategy, *baseline)):
        raise ValueError("bootstrap observations must be finite")

    differences = tuple(left - right for left, right in zip(strategy, baseline))
    if not all(isfinite(value) for value in differences):
        raise ValueError("paired return differences exceed supported numeric range")
    count = len(differences)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(differences[(start + offset) % count] for offset in range(block_size))
        estimates.append(sum(sample[:count]) / count)
    estimates.sort()
    tail = (1 - confidence) / 2
    lower, upper = _quantile(estimates, tail), _quantile(estimates, 1 - tail)
    enough = count >= minimum_observations
    non_degenerate = lower < upper
    return BootstrapInterval(
        mean_difference=sum(differences) / count,
        lower=lower,
        upper=upper,
        observations=count,
        block_size=block_size,
        resamples=resamples,
        seed=seed,
        significant=enough and non_degenerate and (lower > 0 or upper < 0),
        status=(
            "insufficient_sample"
            if not enough
            else "preliminary_only"
            if non_degenerate
            else "degenerate_bootstrap"
        ),
    )
