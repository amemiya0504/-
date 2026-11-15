from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.data.sources import Observation
from gtts.features.institutional import PARTICIPANTS, InstitutionalFeature, institutional_feature
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.strategies.institutional_bayes import (
    VARIANTS,
    Decision,
    InstitutionalModel,
    Opportunity,
    Outcome,
    compare_institutional,
    decide,
    fit_institutional,
    opportunity,
    session_outcome,
)

D = Decimal
MONTH = "202612"


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


@pytest.fixture
def schedules() -> Schedules:
    return Schedules(
        costs={
            "MTX": (
                Costs(
                    effective_from=date(2026, 1, 1),
                    broker_fee_per_contract=D(2),
                    tax_rate=D(".00002"),
                    slippage_ticks=1,
                ),
            ),
        },
        margins={"MTX": (Margin(effective_from=at(1, 0), initial=D(12000), maintenance=D(10000)),)},
    )


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TAIPEI)


def observations(
    calendar: TradingCalendar, source_date: date, changes: list[int]
) -> list[Observation]:
    dates = [source_date]
    for _ in changes:
        dates.append(calendar.previous_trade_date(dates[-1]))
    level = 0
    result = []
    for index, day in enumerate(reversed(dates)):
        if index:
            level += changes[index - 1]
        session = calendar.day_session("TX", day)
        delay = calendar.day_session("TX", calendar.next_trade_date(day))
        assert session is not None and delay is not None
        result.extend(
            Observation(
                source_id="taifex-institutional",
                instrument_id="TX",
                trade_date=day,
                event_at=session.closes_at,
                ingested_at=session.closes_at + timedelta(minutes=5),
                available_at=delay.opens_at,
                availability_estimated=True,
                participant=participant,
                source_revision="original",
                checksum="original",
                values={"open_interest_net": level * (i + 1)},
            )
            for i, participant in enumerate(PARTICIPANTS)
        )
    return result


def bar(
    start: datetime, price: int = 1000, *, close: int | None = None, volume: int = 10
) -> FeatureEvent:
    end = start + timedelta(minutes=5)
    close = price if close is None else close
    return FeatureEvent(
        instrument_id="MTX",
        contract_month=MONTH,
        trade_date=start.date(),
        session="day",
        bar_start=start,
        bar_end=end,
        available_at=end,
        open_price_available_at=start,
        open_volume=volume,
        open_volume_available_at=start,
        values={
            "open": price,
            "close": close,
            "low": min(price, close) - 2,
            "high": max(price, close) + 2,
            "volume": volume,
        },
    )


def training_rows() -> list[Outcome]:
    rows = []
    for i, day in enumerate((2, 3, 4, 5, 6, 9, 10, 11)):
        side = 1 if i % 2 else -1
        first = bar(at(day, 8, 45), close=1000 + side)
        score = side * (2.5 + i / 10)
        feature = InstitutionalFeature(
            date(2026, 11, day - 1),
            first.available_at,
            first.available_at,
            (score, score, score),
            D(side),
            (),
        )
        candidate = Opportunity(feature, first, D("5.1"), D(side), D(100), D(100), at(day, 13, 40))
        rows.append(Outcome(candidate, at(day, 13, 45), D(side * 2000), D(100), D(100)))
    return rows


def positive_oos_candidate() -> tuple[InstitutionalModel, Opportunity]:
    trained = training_rows()
    model = fit_institutional(trained, train_before=at(13, 0), min_fit=4)
    assert model is not None
    first = bar(at(13, 8, 45), close=1001)
    template = trained[-1].opportunity
    candidate = replace(
        template,
        bar=first,
        feature=replace(
            template.feature,
            source_date=date(2026, 11, 12),
            decision_at=first.available_at,
            available_at=first.available_at,
        ),
        exit_at=at(13, 13, 40),
    )
    return model, candidate


def test_past_only_sixty_changes_and_revision_gate(calendar: TradingCalendar) -> None:
    day = date(2026, 11, 12)
    rows = observations(calendar, day, [1, 3] * 30 + [5])
    query = {"source_date": day, "decision_at": at(13, 8, 45)}
    feature = institutional_feature(rows, calendar, **query)
    assert feature is not None
    assert feature.scores == (3, 3, 3)
    assert feature.available_at == at(13, 8, 45)
    assert len(feature.source_rows) == 62 * 3
    original = rows[-3]
    delayed = original.model_copy(
        update={
            "published_at": at(13, 10),
            "ingested_at": at(13, 10),
            "available_at": at(13, 10),
            "source_revision": "revision",
            "values": {"open_interest_net": 1000},
        }
    )
    assert institutional_feature([delayed, *rows], calendar, **query) == feature
    revised = institutional_feature(
        [delayed, *rows], calendar, source_date=day, decision_at=at(13, 10)
    )
    assert revised is not None
    assert revised.scores[0] != 3
    assert revised.scores[1:] == (3, 3)
    assert institutional_feature(rows[:-1], calendar, **query) is None


def test_release_gate_even_for_manually_backdated_rows(calendar: TradingCalendar) -> None:
    day = date(2026, 11, 12)
    rows = observations(calendar, day, [1, 3, 5])
    after_close = at(12, 14)
    last_three = [
        row.model_copy(update={"ingested_at": after_close, "available_at": after_close})
        for row in rows[-3:]
    ]
    assert (
        institutional_feature(
            rows[:-3] + last_three,
            calendar,
            source_date=day,
            decision_at=after_close,
            lookback=2,
            allow_early_publication=True,
        )
        is None
    )
    verified = [
        row.model_copy(update={"published_at": after_close, "availability_estimated": False})
        for row in last_three
    ]
    assert (
        institutional_feature(
            rows[:-3] + verified,
            calendar,
            source_date=day,
            decision_at=after_close - timedelta(microseconds=1),
            lookback=2,
            allow_early_publication=True,
        )
        is None
    )
    assert (
        institutional_feature(
            rows[:-3] + verified, calendar, source_date=day, decision_at=after_close, lookback=2
        )
        is None
    )
    known = institutional_feature(
        rows[:-3] + verified,
        calendar,
        source_date=day,
        decision_at=after_close,
        lookback=2,
        allow_early_publication=True,
    )
    assert known is not None
    assert known.available_at == after_close


def test_calibration_and_train_oos_isolation() -> None:
    rows = training_rows()
    train_before = at(10, 12)
    assert fit_institutional(rows, train_before=train_before, min_fit=4) is None
    train_before = at(12, 8)
    model = fit_institutional(rows, train_before=train_before, min_fit=4)
    assert model is not None
    assert model.fit_count == 4
    assert model.calibration.count == 4
    assert model.calibration.brier is not None
    assert 0 <= model.calibration.brier <= 1
    assert sum(bin.count for bin in model.calibration.bins) == 4

    late = replace(rows[0], available_at=train_before, gross=D(-10_000_000))
    assert fit_institutional([*rows, late], train_before=train_before, min_fit=4) == model
    premature = replace(rows[0], available_at=rows[0].opportunity.exit_at + timedelta(minutes=1))
    with pytest.raises(ValueError, match="invalid institutional target"):
        fit_institutional([premature, *rows[1:]], train_before=train_before, min_fit=4)
    assert fit_institutional(reversed(rows), train_before=train_before, min_fit=4) == model
    assert decide(rows[-1].opportunity, model, "institutional_bayes").order is None
    assert decide(rows[-1].opportunity, None, "institutional_bayes").order is None
    for probability in (D("1.01"), D("-0.01"), D("NaN")):
        with pytest.raises(ValidationError):
            Decision(probability=probability)


def test_orders_apply_probability_cost_and_tick_limits() -> None:
    rows = training_rows()
    template = rows[-1].opportunity
    model = fit_institutional(rows, train_before=at(12, 8), min_fit=4)
    assert model is not None
    first = bar(at(13, 8, 45))
    template = replace(
        template,
        feature=replace(template.feature, decision_at=first.available_at),
        bar=first,
        exit_at=at(13, 13, 40),
    )
    for score, side in ((3.0, "buy"), (-3.0, "sell")):
        candidate = replace(template, feature=replace(template.feature, scores=(score,) * 3))
        prediction = decide(candidate, model, "institutional_bayes")
        assert prediction.probability is not None
        assert D(0) <= prediction.probability <= D(1)
        assert prediction.order is not None
        assert prediction.order.signal.side == side
        assert prediction.order.stop_distance == D("7.65")
        assert prediction.order.exit_at == at(13, 13, 40)
        too_costly = replace(candidate, long_cost=D(100_000), short_cost=D(100_000))
        assert decide(too_costly, model, "institutional_bayes").order is None


def test_session_outcome_and_cost_matched_oos_execution(
    calendar: TradingCalendar, schedules: Schedules
) -> None:
    source_date = date(2026, 11, 12)
    rows = observations(calendar, source_date, [1, 3, 5])
    first = bar(at(13, 8, 45), close=1001)
    history = [bar(at(12, 12, 35) + timedelta(minutes=5 * i)) for i in range(14)]
    candidate = opportunity(rows, history, first, calendar, schedules, lookback=2)
    assert candidate is not None
    missing_first_open = first.model_copy(update={"open_volume": None})
    price_estimate = opportunity(rows, history, missing_first_open, calendar, schedules, lookback=2)
    assert price_estimate is not None and price_estimate.long_cost == candidate.long_cost
    assert candidate.feature.scores == (3, 3, 3)
    assert candidate.atr == D(57) / D(14)
    assert opportunity(rows, history[1:], first, calendar, schedules, lookback=2) is None
    late = first.model_copy(update={"available_at": first.available_at + timedelta(minutes=1)})
    assert opportunity(rows, history, late, calendar, schedules, lookback=2) is None

    entry, exit = bar(at(13, 8, 50)), bar(at(13, 13, 40), 1050, close=1010)
    label = session_outcome(candidate, entry, exit, calendar, schedules)
    assert label is not None and label.up
    assert (label.available_at, label.gross, label.net("buy")) == (
        at(13, 13, 45),
        D(2500),
        D("2393.95000"),
    )
    assert session_outcome(candidate, entry, bar(at(13, 13, 35)), calendar, schedules) is None
    assert (
        session_outcome(candidate, entry, bar(at(13, 13, 40), volume=0), calendar, schedules)
        is None
    )
    model = fit_institutional(training_rows(), train_before=at(13, 0), min_fit=4)
    assert model is not None
    report = compare_institutional(
        model,
        [candidate],
        [*history, first, entry, exit],
        calendar,
        schedules,
        D(100_000),
        test_before=at(13, 14),
    )
    assert tuple(report.runs) == VARIANTS
    assert report.calibration["institutional_bayes"].count == 1
    for result in report.runs.values():
        fills = result.fills
        assert [(row.side, row.reason) for row in fills] == [
            ("buy", "signal"),
            ("sell", "time_exit"),
        ]
        assert fills[1].reference_price == 1050
        assert fills[0].fill_at > candidate.feature.decision_at
        assert fills[1].fill_at > fills[1].signal_at
        assert result.snapshots[-1].positions == {}
        assert result.snapshots[-1].equity - D(100_000) == label.net("buy")

    actual_exit = bar(at(13, 13, 40))
    actual_label = session_outcome(candidate, entry, actual_exit, calendar, schedules)
    assert actual_label is not None and not actual_label.up and label.up
    actual = compare_institutional(
        model,
        [candidate],
        [*history, first, entry, actual_exit],
        calendar,
        schedules,
        D(100_000),
        test_before=at(13, 14),
    )
    prediction = actual.decisions["institutional_bayes"][0].probability
    assert prediction is not None and prediction > D("0.60")
    assert actual.runs["institutional_bayes"].fills[-1].reference_price == 1000
    assert actual.calibration["institutional_bayes"].brier == pytest.approx(float(prediction) ** 2)

    no_capacity = actual_exit.model_copy(update={"open_volume_available_at": None})
    assert session_outcome(candidate, entry, no_capacity, calendar, schedules) is None
    late_entry = entry.model_copy(update={"open_volume_available_at": entry.bar_end})
    assert session_outcome(candidate, late_entry, exit, calendar, schedules) is None
    unpriced_entry = entry.model_copy(update={"open_price_available_at": None})
    assert session_outcome(candidate, unpriced_entry, exit, calendar, schedules) is None
    unlabelled = compare_institutional(
        model,
        [candidate],
        [*history, first, entry, no_capacity],
        calendar,
        schedules,
        D(100_000),
        test_before=at(13, 14),
    )
    assert unlabelled.calibration["institutional_bayes"].count == 0
    assert [fill.reason for fill in unlabelled.runs["institutional_bayes"].fills] == ["signal"]


@pytest.mark.parametrize("cutoff", [at(13, 13, 42), at(13, 14)])
def test_oos_uses_observed_exit_open_without_post_cutoff_data(
    calendar: TradingCalendar, schedules: Schedules, cutoff: datetime
) -> None:
    model, candidate = positive_oos_candidate()
    entry = bar(at(13, 8, 50))
    exit = bar(at(13, 13, 40), 1050).model_copy(update={"available_at": at(13, 14, 30)})
    poisoned_exit = exit.model_copy(
        update={
            "values": {
                **exit.values,
                "close": 999999,
                "high": None,
                "low": 1,
                "volume": 0,
                "halted": 2,
            }
        }
    )
    reports = [
        compare_institutional(
            model,
            [candidate],
            [candidate.bar, entry, closing],
            calendar,
            schedules,
            D(100_000),
            test_before=cutoff,
        )
        for closing in (exit, poisoned_exit)
    ]
    assert reports[0].runs == reports[1].runs
    for report in reports:
        assert report.calibration["institutional_bayes"].count == 0
        result = report.runs["institutional_bayes"]
        assert [(fill.reason, fill.reference_price) for fill in result.fills] == [
            ("signal", 1000),
            ("time_exit", 1050),
        ]
        assert result.fills[-1].fill_at == at(13, 13, 40) + timedelta(microseconds=1)
        assert not result.audit
        assert result.snapshots[-1].snapshot_at < cutoff


def test_oos_reprices_the_decision_cost_gate_with_current_schedules(
    calendar: TradingCalendar, schedules: Schedules
) -> None:
    model, candidate = positive_oos_candidate()
    bars = [candidate.bar, bar(at(13, 8, 50)), bar(at(13, 13, 40), 1050)]
    expensive = schedules.model_copy(
        update={
            "costs": {
                "MTX": (
                    schedules.costs["MTX"][0].model_copy(
                        update={"broker_fee_per_contract": D(2500)}
                    ),
                )
            }
        }
    )
    expensive_run = compare_institutional(
        model, [candidate], bars, calendar, expensive, D(100_000), test_before=at(13, 14)
    )
    for variant in VARIANTS:
        denied = expensive_run.decisions[variant][0]
        assert denied.round_trip_cost == D("5102.002")
        assert denied.order is None
        assert not expensive_run.runs[variant].fills
    cheaper_run = compare_institutional(
        model,
        [replace(candidate, long_cost=D(5000), short_cost=D(5000))],
        bars,
        calendar,
        schedules,
        D(100_000),
        test_before=at(13, 14),
    )
    for variant in VARIANTS:
        accepted = cheaper_run.decisions[variant][0]
        assert accepted.round_trip_cost == D("106.002")
        assert accepted.order is not None
        assert len(cheaper_run.runs[variant].fills) == 2


def test_missing_comparison_cost_fails_closed(
    calendar: TradingCalendar, schedules: Schedules
) -> None:
    model, candidate = positive_oos_candidate()
    no_costs = Schedules(costs={}, margins=schedules.margins)
    result = compare_institutional(
        model,
        [candidate],
        [candidate.bar, bar(at(13, 8, 50)), bar(at(13, 13, 40), 1050)],
        calendar,
        no_costs,
        D(100_000),
        test_before=at(13, 14),
    )
    for variant in VARIANTS:
        decision = result.decisions[variant][0]
        assert decision.round_trip_cost is None and decision.order is None
        assert not result.runs[variant].fills
    assert result.calibration["institutional_bayes"].count == 0


def test_oos_cutoff_retains_expiry_audit_without_the_final_full_bar(
    calendar: TradingCalendar, schedules: Schedules
) -> None:
    model, template = positive_oos_candidate()
    first = bar(at(18, 8, 45), close=1001).model_copy(update={"contract_month": "202611"})
    entry = bar(at(18, 8, 50)).model_copy(update={"contract_month": "202611"})
    exit = bar(at(18, 13, 25), volume=1).model_copy(
        update={"contract_month": "202611", "open_volume": 0, "available_at": at(18, 14, 1)}
    )
    candidate = replace(
        template,
        bar=first,
        feature=replace(
            template.feature,
            source_date=date(2026, 11, 17),
            decision_at=first.available_at,
            available_at=first.available_at,
        ),
        exit_at=at(18, 13, 25),
    )
    cutoff = at(18, 14)
    report = compare_institutional(
        model,
        [candidate],
        [first, entry, exit],
        calendar,
        schedules,
        D(100_000),
        test_before=cutoff,
    )
    assert report.calibration["institutional_bayes"].count == 0
    result = report.runs["institutional_bayes"]
    assert [fill.reason for fill in result.fills] == ["signal"]
    assert result.snapshots[-1].snapshot_at == at(18, 13, 30)
    assert result.snapshots[-1].positions == {"MTX:202611": 1}
    assert "expiry_unsettled" in {row.code for row in result.audit}
    assert all(row.at < cutoff for row in result.audit)
