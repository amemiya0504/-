from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from gtts.data.calendar import TradingCalendar
from gtts.data.ingest import ingest_daily
from gtts.data.quality import FeatureEvent, audit_observations, features_at, point_in_time_join
from gtts.data.schema import TAIPEI
from gtts.data.sources import Snapshot, observation
from gtts.data.sources.taifex import TaifexFutures, TaifexInstitutional
from gtts.data.sources.twse import TwseIndex


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


def observations(calendar: TradingCalendar):
    trade_date = date(2026, 11, 13)
    fetched = datetime(2026, 11, 13, 18, tzinfo=TAIPEI)
    return ingest_daily(
        trade_date,
        calendar,
        (TaifexInstitutional(), TaifexFutures(), TwseIndex()),
        None,
        fetch=lambda endpoint, _: (
            (
                Path("tests/fixtures/sources")
                / (
                    "taifex-institutional"
                    if "MajorInstitutional" in endpoint
                    else "twse-index"
                    if "FMTQIK" in endpoint
                    else "taifex-futures"
                )
            )
            .with_suffix(".json")
            .read_bytes(),
            "fixture-v1",
        ),
        now=lambda: fetched,
    ).records


def test_after_close_institutional_data_is_unavailable_at_same_day_open(
    calendar: TradingCalendar,
) -> None:
    records = observations(calendar)
    decision = datetime(2026, 11, 13, 9, tzinfo=TAIPEI)
    assert not [
        item
        for item in point_in_time_join(records, decision, calendar)
        if item.source_id == "taifex-institutional"
    ]


def test_unfinished_five_minute_bar_cannot_be_used_early() -> None:
    with pytest.raises(ValidationError, match="unavailable before"):
        FeatureEvent(
            instrument_id="TX",
            trade_date=date(2026, 11, 13),
            session="day",
            bar_start=datetime(2026, 11, 13, 9, 55, tzinfo=TAIPEI),
            bar_end=datetime(2026, 11, 13, 10, tzinfo=TAIPEI),
            available_at=datetime(2026, 11, 13, 9, 59, tzinfo=TAIPEI),
        )
    bar = FeatureEvent(
        instrument_id="TX",
        trade_date=date(2026, 11, 13),
        session="day",
        bar_start=datetime(2026, 11, 13, 9, 55, tzinfo=TAIPEI),
        bar_end=datetime(2026, 11, 13, 10, tzinfo=TAIPEI),
        available_at=datetime(2026, 11, 13, 10, tzinfo=TAIPEI),
    )
    calendar = TradingCalendar.from_yaml(Path("configs/contracts.yaml"))
    assert features_at([bar], datetime(2026, 11, 13, 9, 59, tzinfo=TAIPEI), calendar) == ()


def test_wrong_trade_date_night_feature_is_not_available(calendar: TradingCalendar) -> None:
    night = FeatureEvent(
        instrument_id="TX",
        trade_date=date(2026, 11, 12),
        session="night",
        bar_start=datetime(2026, 11, 13, 0, 55, tzinfo=TAIPEI),
        bar_end=datetime(2026, 11, 13, 1, tzinfo=TAIPEI),
        available_at=datetime(2026, 11, 13, 1, tzinfo=TAIPEI),
    )
    assert features_at([night], datetime(2026, 11, 13, 1, tzinfo=TAIPEI), calendar) == ()


@pytest.mark.parametrize(
    "start,end,session,trade_day",
    [
        (datetime(2026, 11, 13, 8, 40), datetime(2026, 11, 13, 8, 45), "day", date(2026, 11, 13)),
        (datetime(2026, 11, 12, 14, 55), datetime(2026, 11, 12, 15), "night", date(2026, 11, 13)),
    ],
)
def test_bar_must_fit_inside_session(start, end, session, trade_day, calendar) -> None:
    bar = FeatureEvent(
        instrument_id="TX",
        trade_date=trade_day,
        session=session,
        bar_start=start.replace(tzinfo=TAIPEI),
        bar_end=end.replace(tzinfo=TAIPEI),
        available_at=end.replace(tzinfo=TAIPEI),
    )
    assert features_at([bar], datetime(2026, 11, 13, 9, tzinfo=TAIPEI), calendar) == ()


def test_night_bar_with_wrong_trade_date_is_rejected(calendar: TradingCalendar) -> None:
    night = next(item for item in observations(calendar) if item.session == "night")
    wrongly_dated = night.model_copy(update={"trade_date": date(2026, 11, 12)})
    audit = audit_observations([wrongly_dated], calendar)
    assert audit.clean == ()
    assert [issue.rule for issue in audit.issues] == ["session_boundary"]
    with pytest.raises(ValueError, match="session/trade_date mismatch"):
        point_in_time_join([wrongly_dated], datetime(2026, 11, 16, 9, tzinfo=TAIPEI), calendar)


def test_point_in_time_revision_uses_latest_version_as_of_decision(
    calendar: TradingCalendar,
) -> None:
    record = next(
        item for item in observations(calendar) if item.source_id == "taifex-institutional"
    )
    revised = record.model_copy(
        update={
            "available_at": datetime(2026, 11, 17, 8, 45, tzinfo=TAIPEI),
            "source_revision": "fixture-v2",
        }
    )
    before_revision = point_in_time_join(
        [record, revised], datetime(2026, 11, 16, 9, tzinfo=TAIPEI), calendar
    )
    after_revision = point_in_time_join(
        [record, revised], datetime(2026, 11, 17, 9, tzinfo=TAIPEI), calendar
    )
    assert before_revision == [record]
    assert after_revision == [revised]


def _bar(
    base,
    minute: int,
    close: str,
    *,
    source: str = "src",
    revision: str = "v1",
    available=None,
    ingested=None,
):
    return base.model_copy(
        update={
            "source_id": source,
            "event_at": datetime(2026, 11, 13, 9, minute, tzinfo=TAIPEI),
            "available_at": available or datetime(2026, 11, 13, 9, minute, tzinfo=TAIPEI),
            "ingested_at": ingested or datetime(2026, 11, 13, 9, minute, tzinfo=TAIPEI),
            "source_revision": revision,
            "values": {
                "open": Decimal(close),
                "high": Decimal(close),
                "low": Decimal(close),
                "close": Decimal(close),
                "volume": 1,
            },
        }
    )


def test_taiex_audit_uses_cash_close_without_calendar_session_error(calendar) -> None:
    index = next(item for item in observations(calendar) if item.instrument_id == "TAIEX")
    assert audit_observations([index], calendar).clean == (index,)
    assert point_in_time_join([index], index.available_at, calendar) == [index]


def test_same_observation_instance_duplicate_keeps_one_and_reports_second_row(calendar) -> None:
    index = next(item for item in observations(calendar) if item.instrument_id == "TAIEX")
    audit = audit_observations([index, index], calendar)
    assert audit.clean == (index,)
    assert [(issue.row, issue.rule) for issue in audit.issues] == [(2, "duplicate")]


@pytest.mark.parametrize(
    "trade_day,session",
    [(date(2026, 11, 14), "day"), (date(2026, 11, 13), "night")],
)
def test_taiex_rejects_non_trading_or_night_session(calendar, trade_day, session) -> None:
    index = next(item for item in observations(calendar) if item.instrument_id == "TAIEX")
    invalid = index.model_copy(update={"trade_date": trade_day, "session": session})
    assert audit_observations([invalid], calendar).clean == ()
    with pytest.raises(ValueError, match="TAIEX event"):
        point_in_time_join([invalid], invalid.available_at, calendar)


def test_audit_validates_institutional_close_and_keeps_calendar_gaps_visible(calendar) -> None:
    records = observations(calendar)
    institutional = next(item for item in records if item.source_id == "taifex-institutional")
    wrong_time = institutional.model_copy(
        update={"event_at": datetime(2026, 11, 13, 9, tzinfo=TAIPEI)}
    )
    assert audit_observations([wrong_time], calendar).clean == ()
    assert audit_observations([wrong_time], calendar).issues[0].rule == "session_boundary"
    index = next(item for item in records if item.instrument_id == "TAIEX")
    for outside in (date(2025, 11, 13), date(2027, 11, 13)):
        expired = index.model_copy(
            update={
                "trade_date": outside,
                "event_at": datetime.combine(outside, calendar.cash_close, TAIPEI),
                "available_at": datetime(2026, 11, 17, 8, 45, tzinfo=TAIPEI),
            }
        )
        result = audit_observations([index, expired], calendar, decision_at=index.available_at)
        assert result.clean == (index,)
        assert result.issues == ()
        later = audit_observations([expired], calendar)
        assert later.clean == () and later.issues[0].row == 1


def test_revision_versions_do_not_make_cross_source_gaps_or_outliers(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    first = _bar(base, 0, "100")
    later = _bar(base, 10, "100")
    other_source = _bar(base, 5, "1000", source="other")
    audit = audit_observations(
        [first, later, other_source],
        calendar,
        interval=timedelta(minutes=5),
        max_price_change=Decimal("0.2"),
    )
    assert [(issue.row, issue.rule) for issue in audit.issues] == [(2, "gap")]


def test_revisions_of_adjacent_bars_still_form_one_gap_and_price_series(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    rows = [
        _bar(base, 0, "100", revision="v1"),
        _bar(base, 5, "200", revision="v2"),
        _bar(base, 15, "105", revision="v3"),
    ]
    audit = audit_observations(
        rows,
        calendar,
        interval=timedelta(minutes=5),
        max_price_change=Decimal("0.2"),
    )
    assert [(issue.row, issue.rule) for issue in audit.issues] == [
        (2, "outlier"),
        (3, "gap"),
    ]


def test_conflicting_duplicate_revision_excludes_both_rows(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    first = _bar(base, 0, "100")
    conflict = first.model_copy(update={"values": {"close": Decimal(101)}})
    audit = audit_observations([first, conflict], calendar)
    assert audit.clean == ()
    assert [(issue.row, issue.rule) for issue in audit.issues] == [
        (1, "duplicate_conflict"),
        (2, "duplicate_conflict"),
    ]


def test_conflicting_latest_revision_excludes_only_its_event(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    first_v1 = _bar(base, 0, "100", revision="v1")
    first_v2 = _bar(base, 0, "200", revision="v2")
    normal = _bar(base, 5, "105", revision="v1")
    audit = audit_observations(
        [first_v1, first_v2, normal],
        calendar,
        interval=timedelta(minutes=5),
        max_price_change=Decimal("0.2"),
    )
    assert audit.clean == (normal,)
    assert [(issue.row, issue.rule) for issue in audit.issues] == [
        (1, "revision_conflict"),
        (2, "revision_conflict"),
    ]


def test_invalid_latest_revision_does_not_fall_back_to_old_version(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    old = _bar(base, 0, "100", revision="v1")
    new = old.model_copy(
        update={
            "available_at": old.available_at + timedelta(minutes=1),
            "ingested_at": old.ingested_at + timedelta(minutes=1),
            "source_revision": "v2",
            "values": {
                "open": Decimal(120),
                "high": Decimal(110),
                "low": Decimal(90),
                "close": Decimal(100),
            },
        }
    )
    audit = audit_observations([old, new], calendar)
    assert audit.clean == ()
    assert [(issue.row, issue.rule) for issue in audit.issues] == [(2, "outlier")]


def test_price_outlier_does_not_become_next_comparison_baseline(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    rows = [_bar(base, 0, "100"), _bar(base, 5, "200"), _bar(base, 10, "105")]
    audit = audit_observations(rows, calendar, max_price_change=Decimal("0.2"))
    assert [(issue.row, issue.rule) for issue in audit.issues] == [(2, "outlier")]
    assert audit.clean == (rows[0], rows[2])


def test_max_price_change_works_without_gap_interval(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    rows = [_bar(base, 0, "100"), _bar(base, 5, "200")]
    audit = audit_observations(rows, calendar, max_price_change=Decimal("0.2"))
    assert [(issue.row, issue.rule) for issue in audit.issues] == [(2, "outlier")]


def test_open_must_be_inside_high_low(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    bad = _bar(base, 0, "100").model_copy(
        update={
            "values": {
                "open": Decimal(120),
                "high": Decimal(110),
                "low": Decimal(90),
                "close": Decimal(100),
            }
        }
    )
    result = audit_observations([bad], calendar)
    assert result.clean == ()
    assert result.issues[0].detail == "open is outside high-low range"


def test_features_at_orders_contract_months(calendar) -> None:
    end = datetime(2026, 11, 13, 10, tzinfo=TAIPEI)
    events = [
        FeatureEvent(
            instrument_id="TX",
            trade_date=date(2026, 11, 13),
            session="day",
            contract_month=month,
            bar_start=end - timedelta(minutes=5),
            bar_end=end,
            available_at=end,
        )
        for month in ("202612", "202701")
    ]
    assert [event.contract_month for event in features_at(events, end, calendar)] == [
        "202612",
        "202701",
    ]


def test_revision_rank_uses_ingestion_and_ignores_superseded_conflict(calendar) -> None:
    base = next(item for item in observations(calendar) if item.source_id == "taifex-institutional")
    available = datetime(2026, 11, 17, 8, 45, tzinfo=TAIPEI)
    older = base.model_copy(
        update={
            "available_at": available,
            "published_at": available,
            "ingested_at": available,
            "values": {"close": Decimal(1)},
            "source_revision": "old",
        }
    )
    newer = base.model_copy(
        update={
            "available_at": available,
            "published_at": available,
            "ingested_at": available.replace(minute=46),
            "values": {"close": Decimal(2)},
            "source_revision": "new",
        }
    )
    decision = datetime(2026, 11, 17, 9, tzinfo=TAIPEI)
    assert point_in_time_join([older, newer], decision, calendar) == [newer]
    early_conflict = base.model_copy(
        update={
            "available_at": available,
            "published_at": available,
            "ingested_at": available,
            "values": {"close": Decimal(3)},
            "source_revision": "conflict-a",
        }
    )
    superseding = base.model_copy(
        update={
            "available_at": available,
            "published_at": available,
            "ingested_at": available.replace(minute=47),
            "values": {"close": Decimal(4)},
            "source_revision": "final",
        }
    )
    assert point_in_time_join([older, early_conflict, superseding], decision, calendar) == [
        superseding
    ]


def test_pit_and_audit_choose_same_provenance_independent_of_input_order(calendar) -> None:
    unknown = next(
        item for item in observations(calendar) if item.source_id == "taifex-institutional"
    )
    known = unknown.model_copy(
        update={
            "published_at": datetime(2026, 11, 13, 13, 46, tzinfo=TAIPEI),
            "availability_estimated": False,
        }
    )
    decision = datetime(2026, 11, 17, 9, tzinfo=TAIPEI)
    for rows in ([unknown, known], [known, unknown]):
        assert point_in_time_join(rows, decision, calendar) == [known]
        assert audit_observations(rows, calendar, decision_at=decision).clean == (known,)


def test_unknown_publication_fallback_delay_is_configurable(calendar) -> None:
    fetched = datetime(2026, 11, 13, 14, tzinfo=TAIPEI)
    snapshot = Snapshot(date(2026, 11, 13), b"raw", "checksum", fetched)
    kwargs = {
        "source_id": "twse-index",
        "instrument_id": "TAIEX",
        "event_at": datetime(2026, 11, 13, 13, 30, tzinfo=TAIPEI),
        "values": {"close": Decimal(22000)},
    }
    default = observation(snapshot, calendar, **kwargs)
    delayed = observation(
        snapshot, calendar, **kwargs, unknown_publication_delay=timedelta(hours=2)
    )
    assert default.availability_estimated
    assert default.available_at == datetime(2026, 11, 16, 8, 45, tzinfo=TAIPEI)
    assert delayed.available_at == datetime(2026, 11, 16, 10, 45, tzinfo=TAIPEI)


def test_audit_excludes_future_revision_from_early_outlier_check(calendar) -> None:
    base = next(
        item
        for item in observations(calendar)
        if item.instrument_id == "TX" and item.session == "day"
    )
    first = _bar(base, 0, "100")
    second = _bar(base, 5, "105")
    future = second.model_copy(
        update={
            "available_at": datetime(2026, 11, 18, 8, 45, tzinfo=TAIPEI),
            "ingested_at": datetime(2026, 11, 18, 8, 45, tzinfo=TAIPEI),
            "source_revision": "future-v2",
            "values": {"close": Decimal(500)},
        }
    )
    audit = audit_observations(
        [first, second, future],
        calendar,
        max_price_change=Decimal("0.2"),
        decision_at=datetime(2026, 11, 17, 9, tzinfo=TAIPEI),
    )
    assert audit.raw_count == 3 and len(audit.raw) == 3
    assert audit.clean == (first, second)
    assert audit.issues == ()


def test_default_audit_current_view_keeps_only_latest_revision(calendar) -> None:
    old = next(item for item in observations(calendar) if item.source_id == "taifex-institutional")
    old = old.model_copy(
        update={
            "available_at": datetime(2026, 11, 16, 8, 45, tzinfo=TAIPEI),
            "source_revision": "v1",
        }
    )
    new = old.model_copy(
        update={
            "available_at": datetime(2026, 11, 17, 8, 45, tzinfo=TAIPEI),
            "source_revision": "v2",
            "values": {"close": Decimal(2)},
        }
    )
    audit = audit_observations([old, new], calendar)
    assert audit.raw == (old, new)
    assert audit.clean == (new,)


def test_audit_rejects_naive_decision_at(calendar) -> None:
    with pytest.raises(ValueError, match="Asia/Taipei"):
        audit_observations([], calendar, decision_at=datetime(2026, 11, 17, 9))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"bar_end": datetime(2026, 11, 13, 10, 1, tzinfo=TAIPEI)}, "exactly five minutes"),
        ({"open_price_available_at": datetime(2026, 11, 13, 9, 56, tzinfo=TAIPEI)}, "open price must"),
        ({"open_volume_available_at": datetime(2026, 11, 13, 9, 56, tzinfo=TAIPEI)}, "open volume must"),
        ({"open_volume": -1}, "greater than or equal to 0"),
        ({"bar_start": datetime(2026, 11, 13, 1, tzinfo=UTC)}, "Asia/Taipei"),
    ],
)
def test_feature_event_rejects_invalid_times_and_values(overrides, message) -> None:
    values = {
        "instrument_id": "TX",
        "trade_date": date(2026, 11, 13),
        "session": "day",
        "bar_start": datetime(2026, 11, 13, 9, 55, tzinfo=TAIPEI),
        "bar_end": datetime(2026, 11, 13, 10, tzinfo=TAIPEI),
        "available_at": datetime(2026, 11, 13, 10, tzinfo=TAIPEI),
    }
    with pytest.raises(ValidationError, match=message):
        FeatureEvent(**(values | overrides))


def test_point_in_time_join_rejects_non_taipei_decision(calendar: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="Asia/Taipei"):
        point_in_time_join([], datetime(2026, 11, 17, 1, tzinfo=UTC), calendar)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"max_price_change": Decimal("-0.1")}, "non-negative"),
        ({"interval": timedelta(0)}, "interval must be positive"),
        ({"decision_at": datetime(2026, 11, 17, 1)}, "Asia/Taipei"),
    ],
)
def test_audit_rejects_invalid_safety_limits(calendar: TradingCalendar, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        audit_observations([], calendar, **kwargs)
