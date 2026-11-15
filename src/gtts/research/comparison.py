"""Paired comparisons, reproducible manifests, and rerunnable stress inputs."""

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from gtts.data.quality import FeatureEvent
from gtts.engine.backtest import PositionFeatures, Request
from gtts.engine.events import BacktestResult, Contract
from gtts.research import MINIMUM_INDEPENDENT_TRADES, MINIMUM_RESEARCH_OBSERVATIONS
from gtts.research.bootstrap import BootstrapInterval, paired_block_bootstrap
from gtts.risk.margin import Costs, Schedules


@dataclass(frozen=True)
class PairedComparison:
    start: date
    end: date
    strategy: str
    baseline: str
    completed_trades: int
    interval: BootstrapInterval


def compare_paired_daily_returns(
    rows: Sequence[tuple[date, float, float]],
    *,
    strategy_name: str,
    baseline_name: str,
    block_size: int = 5,
    resamples: int = 2_000,
    confidence: float = 0.95,
    seed: int = 20_261_116,
    minimum_observations: int = MINIMUM_RESEARCH_OBSERVATIONS,
    verified_trade_count: int = 0,
    minimum_trades: int = MINIMUM_INDEPENDENT_TRADES,
) -> PairedComparison:
    if not rows:
        raise ValueError("paired daily returns are required")
    dates = tuple(row[0] for row in rows)
    if dates != tuple(sorted(set(dates))):
        raise ValueError("paired return dates must be unique and chronological")
    if (
        type(minimum_observations) is not int
        or minimum_observations < MINIMUM_RESEARCH_OBSERVATIONS
    ):
        raise ValueError(f"minimum_observations must be at least {MINIMUM_RESEARCH_OBSERVATIONS}")
    if type(minimum_trades) is not int or minimum_trades < MINIMUM_INDEPENDENT_TRADES:
        raise ValueError(f"minimum_trades must be at least {MINIMUM_INDEPENDENT_TRADES}")
    if type(verified_trade_count) is not int or verified_trade_count < 0:
        raise ValueError("verified_trade_count must be a non-negative integer")
    interval = paired_block_bootstrap(
        tuple(row[1] for row in rows),
        tuple(row[2] for row in rows),
        block_size=block_size,
        resamples=resamples,
        confidence=confidence,
        seed=seed,
        minimum_observations=minimum_observations,
    )
    if verified_trade_count < minimum_trades:
        interval = replace(interval, significant=False, status="insufficient_sample")
    return PairedComparison(
        dates[0], dates[-1], strategy_name, baseline_name, verified_trade_count, interval
    )


@dataclass(frozen=True)
class CostStress:
    name: str
    schedules: Schedules


@dataclass(frozen=True)
class StressRun:
    name: str
    result: BacktestResult


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cost_stress_scenarios(
    schedules: Schedules,
    *,
    tick_levels: Sequence[int] = (1, 2, 3),
    fee_multipliers: Sequence[Decimal] = (Decimal("1.5"), Decimal(2)),
) -> tuple[CostStress, ...]:
    """Return fee/slippage variants to pass through the normal strategy runner."""
    if not schedules.costs:
        raise ValueError("cost stress requires at least one effective-dated cost schedule")
    if any(type(ticks) is not int or ticks < 0 for ticks in tick_levels):
        raise ValueError("slippage tick levels must be non-negative integers")
    if any(
        not isinstance(multiplier, Decimal) or not multiplier.is_finite() or multiplier < 1
        for multiplier in fee_multipliers
    ):
        raise ValueError("fee multipliers must be finite Decimal values of at least 1")
    scenarios: list[CostStress] = []
    for ticks in tick_levels:
        for multiplier in fee_multipliers:
            cost_map: dict[str, tuple[Costs, ...]] = {
                instrument: tuple(
                    row.model_copy(
                        update={
                            "slippage_ticks": ticks,
                            "broker_fee_per_contract": row.broker_fee_per_contract * multiplier,
                            "final_settlement_fee_per_contract": (
                                row.final_settlement_fee_per_contract * multiplier
                                if row.final_settlement_fee_per_contract is not None
                                else None
                            ),
                        }
                    )
                    for row in rows
                )
                for instrument, rows in schedules.costs.items()
            }
            scenarios.append(
                CostStress(
                    name=f"slippage_{ticks}_ticks_fee_{multiplier}x",
                    schedules=Schedules(costs=cost_map, margins=schedules.margins),
                )
            )
    return tuple(scenarios)


def delayed_decision_features(features: Iterable[FeatureEvent]) -> tuple[FeatureEvent, ...]:
    """Create late decision events without changing the original execution bars."""
    delay = timedelta(minutes=5)
    return tuple(
        row.model_copy(update={"available_at": row.available_at + delay}) for row in features
    )


def only_delayed_position_decisions(
    callback: PositionFeatures, decisions: Sequence[FeatureEvent]
) -> PositionFeatures:
    """Ignore original feature callbacks and run the strategy only on delayed events."""
    delayed_at = {
        (row.instrument_id, row.contract_month or "", row.bar_start): row.available_at
        for row in decisions
    }

    def delayed_callback(
        feature: FeatureEvent, positions: Mapping[Contract, int]
    ) -> Iterable[Request]:
        key = feature.instrument_id, feature.contract_month or "", feature.bar_start
        if delayed_at.get(key) != feature.available_at:
            return ()
        return callback(feature, positions)

    return delayed_callback


def run_stress_scenarios(
    schedules: Schedules,
    features: Iterable[FeatureEvent],
    runner: Callable[
        [Schedules, tuple[FeatureEvent, ...], tuple[FeatureEvent, ...] | None], BacktestResult
    ],
) -> tuple[StressRun, ...]:
    """Rerun a caller-supplied strategy with six costs and each one-bar delay variant."""
    rows = tuple(features)
    if not rows:
        raise ValueError("stress testing requires complete feature bars")
    delayed = delayed_decision_features(rows)
    runs: list[StressRun] = []
    for scenario in cost_stress_scenarios(schedules):
        runs.append(StressRun(scenario.name, runner(scenario.schedules, rows, None)))
        runs.append(
            StressRun(
                f"{scenario.name}_delay_1_bar",
                runner(scenario.schedules, rows, delayed),
            )
        )
    return tuple(runs)


def build_run_manifest(
    data_files: Iterable[Path],
    *,
    config: Mapping[str, Any],
    seed: int,
    repository: Path,
    data_checksums: Sequence[tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Hash source inputs/config and record the code revision used for a report."""
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    files = tuple(Path(path).resolve() for path in data_files)
    if not files or any(not path.is_file() for path in files):
        raise ValueError("manifest requires existing input data files")
    if data_checksums is not None and (
        len(data_checksums) != len(files)
        or any(
            file_name != path.name
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
            for path, (file_name, checksum) in zip(files, data_checksums)
        )
    ):
        raise ValueError("provided checksums must match input files and be lowercase SHA-256")
    checksums = sorted(
        (
            {
                "file_name": file_name,
                "sha256": checksum,
            }
            for file_name, checksum in data_checksums
        )
        if data_checksums is not None
        else ({"file_name": path.name, "sha256": _file_sha256(path)} for path in files),
        key=lambda item: (str(item["file_name"]), str(item["sha256"])),
    )
    config_bytes = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    repository = repository.resolve()
    root_result = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "--show-toplevel"),
        capture_output=True,
        check=False,
        text=True,
    )
    code_commit: str | None = None
    code_dirty: bool | None = None
    if root_result.returncode == 0:
        git_root = Path(root_result.stdout.strip())
        commit_result = subprocess.run(
            ("git", "-C", str(git_root), "rev-parse", "HEAD"),
            capture_output=True,
            check=False,
            text=True,
        )
        if commit_result.returncode == 0:
            code_commit = commit_result.stdout.strip()
        try:
            project_path = repository.relative_to(git_root).as_posix()
        except ValueError:
            project_path = "."
        status_result = subprocess.run(
            (
                "git",
                "-C",
                str(git_root),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                f"{project_path}/src/gtts",
                f"{project_path}/scripts",
                f"{project_path}/tests",
                f"{project_path}/docs",
                f"{project_path}/configs",
                f"{project_path}/pyproject.toml",
                f"{project_path}/uv.lock",
            ),
            capture_output=True,
            check=False,
            text=True,
        )
        if status_result.returncode == 0:
            code_dirty = bool(status_result.stdout.strip())
    return {
        "data_sha256": checksums,
        "code_commit": code_commit,
        "code_dirty": code_dirty,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "seed": seed,
    }
