"""Research-only institutional direction, calibration, and baseline decisions."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from math import exp, fsum, isfinite, log, sqrt
from statistics import fmean, pvariance
from typing import Literal

from pydantic import Field

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import Schema, SignalEvent, _taipei_datetime
from gtts.data.sources import Observation
from gtts.engine.backtest import run_backtest
from gtts.engine.broker import (
    AFTER,
    INTERVAL,
    Bar,
    CannotFill,
    OpeningBar,
    executable_bar,
    execution_terms,
    opening_bar,
)
from gtts.engine.events import BacktestResult, OrderRequest, SettlementEvent, Side
from gtts.features.institutional import InstitutionalFeature, Scores, institutional_feature
from gtts.features.volatility import atr14
from gtts.risk.margin import Schedules

Variant = Literal["institutional_bayes", "institutional_logistic", "institutional_net", "momentum"]
VARIANTS: tuple[Variant, ...] = (
    "institutional_bayes",
    "institutional_logistic",
    "institutional_net",
    "momentum",
)
SIDES: tuple[Side, ...] = ("buy", "sell")


@dataclass(frozen=True)
class Opportunity:
    feature: InstitutionalFeature
    bar: FeatureEvent
    atr: Decimal
    momentum: Decimal
    long_cost: Decimal | None
    short_cost: Decimal | None
    exit_at: datetime


@dataclass(frozen=True)
class Outcome:
    """Fixed-horizon directional benchmark using one actual MTX contract."""

    opportunity: Opportunity
    available_at: datetime
    gross: Decimal
    long_cost: Decimal
    short_cost: Decimal

    @property
    def up(self) -> bool:
        return self.gross > self.long_cost

    def net(self, side: Side) -> Decimal:
        return self.gross - self.long_cost if side == "buy" else -self.gross - self.short_cost


def _cost_at_price(
    bar: Bar | OpeningBar, side: Side, price: Decimal, schedules: Schedules
) -> Decimal:
    _, fee, tax, slippage = execution_terms(bar, price, side, 1, schedules)
    return fee + tax + slippage


def _round_trip_cost(bar: Bar, schedules: Schedules) -> Decimal:
    return sum((_cost_at_price(bar, side, bar.close, schedules) for side in SIDES), Decimal(0))


def opportunity(
    observations: Iterable[Observation],
    history: Iterable[FeatureEvent],
    first_bar: FeatureEvent,
    calendar: TradingCalendar,
    schedules: Schedules,
    *,
    lookback: int = 60,
    allow_early_publication: bool = False,
) -> Opportunity | None:
    """Form one decision at the first on-time complete MTX day bar."""
    if first_bar.instrument_id != "MTX" or first_bar.session != "day":
        return None
    try:
        bar = executable_bar(first_bar, calendar)
        if (
            not bar.total_volume
            or first_bar.bar_start != bar.session.opens_at
            or first_bar.available_at != first_bar.bar_end
        ):
            return None
        exit_at = bar.session.closes_at - INTERVAL
        if first_bar.available_at >= exit_at:
            return None
        source_date = calendar.previous_trade_date(first_bar.trade_date)
        feature = institutional_feature(
            observations,
            calendar,
            source_date=source_date,
            decision_at=first_bar.available_at,
            lookback=lookback,
            allow_early_publication=allow_early_publication,
        )
        if feature is None:
            return None
        atr = atr14(bar, history, calendar)
        if atr is None or atr <= 0:
            return None
        cost = _round_trip_cost(bar, schedules)
    except (CannotFill, CalendarGap):
        return None
    return Opportunity(feature, first_bar, atr, bar.close - bar.open, cost, cost, exit_at)


def session_outcome(
    candidate: Opportunity,
    entry: FeatureEvent,
    exit: FeatureEvent,
    calendar: TradingCalendar,
    schedules: Schedules,
) -> Outcome | None:
    """Measure the next session's reference interval after both bars are available."""
    if (
        entry.bar_start != candidate.feature.decision_at
        or exit.bar_start != candidate.exit_at
        or any(
            row.session != "day"
            or row.trade_date != candidate.bar.trade_date
            or row.instrument_id != candidate.bar.instrument_id
            or row.contract_month != candidate.bar.contract_month
            for row in (entry, exit)
        )
    ):
        return None
    try:
        entry_bar, exit_bar = opening_bar(entry, calendar), opening_bar(exit, calendar)
        executable_bar(entry, calendar, opened=entry_bar)
        executable_bar(exit, calendar, opened=exit_bar)
        if not (entry_bar.available_volume() and exit_bar.available_volume()):
            return None
        entry_price, exit_price = entry_bar.price, exit_bar.price
        assert entry_price is not None and exit_price is not None
        long_cost = _cost_at_price(entry_bar, "buy", entry_price, schedules) + _cost_at_price(
            exit_bar, "sell", exit_price, schedules
        )
        short_cost = _cost_at_price(entry_bar, "sell", entry_price, schedules) + _cost_at_price(
            exit_bar, "buy", exit_price, schedules
        )
    except CannotFill:
        return None
    return Outcome(
        candidate,
        max(entry.available_at, exit.available_at),
        (exit_price - entry_price) * entry_bar.multiplier,
        long_cost,
        short_cost,
    )


def _sigmoid(value: float) -> float:
    e = exp(-abs(value))
    return 1 / (1 + e) if value >= 0 else e / (1 + e)


@dataclass(frozen=True)
class _Gaussian:
    means: tuple[tuple[float, ...], ...]
    variances: tuple[tuple[float, ...], ...]
    prior_log_odds: float

    @classmethod
    def fit(cls, rows: list[Outcome]) -> "_Gaussian":
        groups = [
            [row.opportunity.feature.scores for row in rows if row.up == up] for up in (False, True)
        ]
        smoothing = 1e-6 * max(
            1.0,
            *(
                pvariance(values)
                for values in zip(*(row.opportunity.feature.scores for row in rows), strict=True)
            ),
        )
        means = tuple(tuple(map(fmean, zip(*group, strict=True))) for group in groups)
        variances = tuple(
            tuple(pvariance(values) + smoothing for values in zip(*group, strict=True))
            for group in groups
        )
        return cls(means, variances, log((len(groups[1]) + 1) / (len(groups[0]) + 1)))

    def score(self, values: Scores) -> float:
        densities = [
            -0.5
            * fsum(
                log(variance) + max(-1e150, min(1e150, (x - mean) / sqrt(variance))) ** 2
                for x, mean, variance in zip(values, means, variances, strict=True)
            )
            for means, variances in zip(self.means, self.variances, strict=True)
        ]
        return max(-100.0, min(100.0, self.prior_log_odds + densities[1] - densities[0]))


@dataclass(frozen=True)
class _Logistic:
    center: tuple[float, ...]
    scale: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float

    @classmethod
    def fit(cls, features: Sequence[tuple[float, ...]], labels: list[bool]) -> "_Logistic":
        columns = tuple(zip(*features, strict=True))
        center = tuple(map(fmean, columns))
        scale = tuple(sqrt(pvariance(col)) or 1.0 for col in columns)
        matrix = [
            tuple((x - m) / s for x, m, s in zip(row, center, scale, strict=True))
            for row in features
        ]
        weights = [0.0] * len(columns)
        intercept = 0.0
        # Fixed-step optimization on training-standardized predictors with L2 regularization.
        for _ in range(400):
            errors = [
                _sigmoid(intercept + fsum(w * x for w, x in zip(weights, row, strict=True))) - y
                for row, y in zip(matrix, labels, strict=True)
            ]
            intercept -= 0.2 * fmean(errors)
            weights = [
                weight
                - 0.2
                * (
                    fmean(error * row[i] for row, error in zip(matrix, errors, strict=True))
                    + 0.01 * weight
                )
                for i, weight in enumerate(weights)
            ]
        return cls(center, scale, tuple(weights), intercept)

    def probability(self, values: tuple[float, ...]) -> float:
        return _sigmoid(
            self.intercept
            + fsum(
                weight * (x - center) / scale
                for x, center, scale, weight in zip(
                    values, self.center, self.scale, self.weights, strict=True
                )
            )
        )


@dataclass(frozen=True)
class CalibrationBin:
    count: int
    mean_probability: float
    positive_rate: float


@dataclass(frozen=True)
class CalibrationReport:
    count: int
    brier: float | None
    bins: tuple[CalibrationBin, ...]


def _calibration_report(points: Iterable[tuple[float, bool]]) -> CalibrationReport:
    rows = list(points)
    buckets: list[list[tuple[float, bool]]] = [[], [], [], [], []]
    for probability, up in rows:
        if not 0 <= probability <= 1:
            raise ValueError("probability is outside [0, 1]")
        buckets[min(4, int(probability * 5))].append((probability, up))
    return CalibrationReport(
        len(rows),
        fmean((p - up) ** 2 for p, up in rows) if rows else None,
        tuple(
            CalibrationBin(len(bucket), fmean(p for p, _ in bucket), fmean(y for _, y in bucket))
            for bucket in buckets
            if bucket
        ),
    )


def _direction(candidate: Opportunity, variant: Variant) -> Side | None:
    value = (
        candidate.feature.foreign_change if variant == "institutional_net" else candidate.momentum
    )
    return "buy" if value > 0 else "sell" if value < 0 else None


def _mean_pnl(rows: Iterable[Decimal]) -> Decimal | None:
    values = tuple(rows)
    return sum(values, Decimal(0)) / len(values) if values else None


@dataclass(frozen=True)
class InstitutionalModel:
    train_before: datetime
    fit_count: int
    calibration: CalibrationReport
    gaussian: _Gaussian
    platt: _Logistic
    logistic: _Logistic
    mean_up: Decimal
    mean_down: Decimal
    direction_gross: Mapping[tuple[Variant, Side], Decimal | None]


def fit_institutional(
    outcomes: Iterable[Outcome], *, train_before: datetime, min_fit: int = 30
) -> InstitutionalModel | None:
    """Fit on a chronological prefix and calibrate on a disjoint training suffix."""
    _taipei_datetime(train_before)
    if min_fit < 4:
        raise ValueError("min_fit must be at least four outcomes")
    rows = _sorted_outcomes(
        row
        for row in outcomes
        if row.available_at < train_before and row.opportunity.feature.decision_at < train_before
    )
    suffix_size = max(4, len(rows) // 5)
    if len(rows) < min_fit + suffix_size:
        return None
    calibration = rows[-suffix_size:]
    calibration_start = calibration[0].opportunity.feature.decision_at
    training = [row for row in rows[:-suffix_size] if row.available_at < calibration_start]
    positives = sum(row.up for row in training)
    if (
        len(training) < min_fit
        or min(positives, len(training) - positives) < 2
        or len({row.up for row in calibration}) < 2
    ):
        return None
    gaussian = _Gaussian.fit(training)
    labels = [row.up for row in calibration]
    scores = [(gaussian.score(row.opportunity.feature.scores),) for row in calibration]
    platt = _Logistic.fit(scores, labels)
    logistic = _Logistic.fit(
        [row.opportunity.feature.scores for row in training], [row.up for row in training]
    )
    mean_up = _mean_pnl(row.gross for row in training if row.up)
    mean_down = _mean_pnl(row.gross for row in training if not row.up)
    assert mean_up is not None and mean_down is not None
    return InstitutionalModel(
        train_before,
        len(training),
        _calibration_report(zip(map(platt.probability, scores), labels, strict=True)),
        gaussian,
        platt,
        logistic,
        mean_up,
        mean_down,
        {
            (variant, side): _mean_pnl(
                row.gross if side == "buy" else -row.gross
                for row in training
                if _direction(row.opportunity, variant) == side
            )
            for variant in VARIANTS[2:]
            for side in SIDES
        },
    )


def _sorted_outcomes(outcomes: Iterable[Outcome]) -> list[Outcome]:
    rows = sorted(outcomes, key=lambda row: row.opportunity.feature.decision_at)
    days = [row.opportunity.bar.trade_date for row in rows]
    if len(days) != len(set(days)):
        raise ValueError("choose one MTX contract per target trading day")
    for row in rows:
        if (
            _taipei_datetime(row.available_at) < row.opportunity.exit_at + INTERVAL
            or not row.gross.is_finite()
            or any(not cost.is_finite() or cost < 0 for cost in (row.long_cost, row.short_cost))
            or not all(map(isfinite, row.opportunity.feature.scores))
        ):
            raise ValueError("invalid institutional target")
    return rows


class Decision(Schema):
    probability: Decimal | None = Field(default=None, ge=0, le=1)
    expected_gross: Decimal | None = None
    round_trip_cost: Decimal | None = None
    order: OrderRequest | None = None


def decide(candidate: Opportunity, model: InstitutionalModel | None, variant: Variant) -> Decision:
    """Create a one-lot MTX entry with a stop and an end-of-session exit."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown institutional strategy variant: {variant}")
    if model is None or candidate.feature.decision_at < model.train_before:
        return Decision()
    probability: Decimal | None = None
    expected: Decimal | None
    if variant in ("institutional_bayes", "institutional_logistic"):
        scores = candidate.feature.scores
        p = (
            model.platt.probability((model.gaussian.score(scores),))
            if variant == "institutional_bayes"
            else model.logistic.probability(scores)
        )
        probability = Decimal(str(p))
        side: Side | None = "buy" if p >= 0.60 else "sell" if p <= 0.40 else None
        gross = probability * model.mean_up + (1 - probability) * model.mean_down
        expected = -gross if side == "sell" else gross
    else:
        side = _direction(candidate, variant)
        expected = model.direction_gross.get((variant, side)) if side is not None else None
    if side is None:
        return Decision(probability=probability)
    cost = candidate.long_cost if side == "buy" else candidate.short_cost
    decision = Decision(probability=probability, expected_gross=expected, round_trip_cost=cost)
    if cost is None or expected is None or expected <= 0 or expected < 2 * cost:
        return decision
    month = candidate.bar.contract_month
    assert month is not None
    available_at = candidate.bar.available_at
    order = OrderRequest(
        signal=SignalEvent(
            signal_id=f"s1:{variant}:{candidate.bar.trade_date}:{month}",
            strategy_id=variant,
            instrument_id="MTX",
            side=side,
            decision_at=candidate.feature.decision_at,
            available_at=max(available_at, candidate.feature.available_at),
            probability=probability,
        ),
        contract_month=month,
        quantity=1,
        stop_distance=Decimal("1.5") * candidate.atr,
        exit_at=candidate.exit_at,
    )
    return decision.model_copy(update={"order": order})


@dataclass(frozen=True)
class InstitutionalComparison:
    """Cost-matched simulated executions and discrimination statistics for the OOS window."""

    opportunities: tuple[Opportunity, ...]
    decisions: Mapping[Variant, tuple[Decision, ...]]
    runs: Mapping[Variant, BacktestResult]
    calibration: Mapping[Variant, CalibrationReport]


def compare_institutional(
    model: InstitutionalModel,
    opportunities: Iterable[Opportunity],
    bars: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    *,
    test_before: datetime,
    settlements: Iterable[SettlementEvent] = (),
) -> InstitutionalComparison:
    """Run S1 and its three baselines on identical OOS inputs and cost schedules."""
    if _taipei_datetime(test_before) <= model.train_before:
        raise ValueError("test_before must be later than the training cutoff")
    eligible = sorted(
        (
            row
            for row in opportunities
            if model.train_before <= row.feature.decision_at < row.exit_at
            and row.bar.available_at <= row.feature.decision_at
            and row.exit_at + AFTER < test_before
        ),
        key=lambda row: row.feature.decision_at,
    )
    rows: list[Opportunity] = []
    for row in eligible:
        cost: Decimal | None
        try:
            cost = _round_trip_cost(executable_bar(row.bar, calendar), schedules)
        except CannotFill:
            cost = None
        rows.append(replace(row, long_cost=cost, short_cost=cost))
    if len(rows) != len({row.bar.trade_date for row in rows}):
        raise ValueError("choose one MTX contract per OOS trading day")
    comparisons = {
        variant: tuple(decide(row, model, variant) for row in rows) for variant in VARIANTS
    }
    window, opening_only = [], []
    for market_bar in bars:
        if (
            model.train_before <= market_bar.bar_start
            and market_bar.bar_start + AFTER < test_before
        ):
            if market_bar.available_at < test_before:
                window.append(market_bar)
            else:
                opening_only.append(market_bar)
    settlements = tuple(
        row for row in settlements if model.train_before <= row.available_at < test_before
    )
    results = {
        variant: run_backtest(
            calendar,
            schedules,
            initial_cash,
            window,
            before=test_before,
            opening_only_bars=opening_only,
            orders=(row.order for row in decisions if row.order is not None),
            settlements=settlements,
        )
        for variant, decisions in comparisons.items()
    }
    by_open = {(bar.instrument_id, bar.contract_month, bar.bar_start): bar for bar in window}
    labels: dict[datetime, Outcome] = {}
    for candidate in rows:
        instrument = candidate.bar.instrument_id
        month = candidate.bar.contract_month
        decision_at = candidate.feature.decision_at
        entry = by_open.get((instrument, month, decision_at))
        exit = by_open.get((instrument, month, candidate.exit_at))
        if entry is not None and exit is not None:
            label = session_outcome(candidate, entry, exit, calendar, schedules)
            if label is not None:
                labels[decision_at] = label

    def calibration(variant_decisions: tuple[Decision, ...]) -> CalibrationReport:
        data = []
        for candidate, decision in zip(rows, variant_decisions, strict=True):
            label = labels.get(candidate.feature.decision_at)
            if decision.probability is not None and label is not None:
                data.append((float(decision.probability), label.up))
        return _calibration_report(data)

    return InstitutionalComparison(
        tuple(rows),
        comparisons,
        results,
        {variant: calibration(comparisons[variant]) for variant in VARIANTS[:2]},
    )
