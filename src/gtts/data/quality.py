"""Point-in-time selection and conservative market-data quality checks."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from gtts.data.calendar import CalendarGap, SessionKind, TradingCalendar
from gtts.data.schema import Schema
from gtts.data.sources import Observation


class FeatureEvent(Schema):
    """A completed five-minute feature bar, usable only after its close."""

    instrument_id: str
    trade_date: date
    session: SessionKind
    contract_month: str | None = None
    bar_start: datetime
    bar_end: datetime
    available_at: datetime
    open_price_available_at: datetime | None = None
    open_volume: int | None = Field(default=None, ge=0, strict=True)
    open_volume_available_at: datetime | None = None
    values: dict[str, Decimal | int | None] = Field(default_factory=dict)

    @classmethod
    def _validate_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(hours=8):
            raise ValueError("timestamps must use Asia/Taipei (+08:00)")
        return value

    @field_validator("bar_start", "bar_end", "available_at")
    @classmethod
    def timestamps_use_taipei(cls, value: datetime) -> datetime:
        return cls._validate_times(value)

    @field_validator("open_price_available_at", "open_volume_available_at")
    @classmethod
    def opening_timestamp_uses_taipei(cls, value: datetime | None) -> datetime | None:
        return cls._validate_times(value) if value is not None else None

    @model_validator(mode="after")
    def bar_is_complete(self) -> "FeatureEvent":
        if self.bar_end - self.bar_start != timedelta(minutes=5):
            raise ValueError("feature bar must span exactly five minutes")
        if self.available_at < self.bar_end:
            raise ValueError("feature is unavailable before the five-minute bar closes")
        if (
            self.open_price_available_at is not None
            and self.open_price_available_at > self.bar_start
        ):
            raise ValueError("open price must be available by the five-minute bar start")
        if (
            self.open_volume_available_at is not None
            and self.open_volume_available_at > self.bar_start
        ):
            raise ValueError("open volume must be available by the five-minute bar start")
        return self


def _revision_winner_key(item: Observation) -> tuple[bool, str, bool, str, str, str]:
    return (
        item.published_at is not None,
        item.published_at.isoformat() if item.published_at is not None else "",
        not item.availability_estimated,
        item.source_revision or "",
        item.checksum or "",
        repr(sorted(item.values.items())),
    )


def point_in_time_join(
    observations: Iterable[Observation], decision_at: datetime, calendar: TradingCalendar
) -> list[Observation]:
    """Return each logical observation's latest revision available at decision_at."""
    if decision_at.tzinfo is None or decision_at.utcoffset() != timedelta(hours=8):
        raise ValueError("decision_at must use Asia/Taipei (+08:00)")
    versions: dict[tuple[object, ...], list[Observation]] = defaultdict(list)
    for item in observations:
        if item.available_at > decision_at:
            continue
        key = (
            item.source_id,
            item.instrument_id,
            item.contract_month,
            item.trade_date,
            item.event_at,
            item.session,
            item.participant,
        )
        versions[key].append(item)
    selected: list[Observation] = []
    for logical_key, candidates in versions.items():
        latest_rank = max((item.available_at, item.ingested_at) for item in candidates)
        latest = [
            item for item in candidates if (item.available_at, item.ingested_at) == latest_rank
        ]
        if any(item.values != latest[0].values for item in latest[1:]):
            raise ValueError(f"ambiguous revisions available at {decision_at}: {logical_key}")
        item = max(latest, key=_revision_winner_key)
        if item.instrument_id == "TAIEX":
            if (
                not calendar.is_trade_date(item.trade_date)
                or item.session != "day"
                or item.event_at.date() != item.trade_date
                or item.event_at.time() != calendar.cash_close
            ):
                raise ValueError(
                    f"TAIEX event does not match its trade date and cash close: {logical_key}"
                )
        elif item.session is not None:
            session = calendar.session_at(item.instrument_id, item.event_at, item.contract_month)
            if (
                session is None
                or session.kind != item.session
                or session.trade_date != item.trade_date
            ):
                raise ValueError(f"observation session/trade_date mismatch: {logical_key}")
        elif item.instrument_id in calendar.contracts:
            session = calendar.day_session(item.instrument_id, item.trade_date, item.contract_month)
            if session is None or item.event_at != session.closes_at:
                raise ValueError(
                    f"daily observation does not match day-session close: {logical_key}"
                )
        else:
            raise ValueError(f"unsupported observation instrument: {item.instrument_id}")
        selected.append(item)
    return sorted(
        selected,
        key=lambda item: (
            item.event_at,
            item.source_id,
            item.instrument_id,
            item.contract_month or "",
            item.session or "",
            item.participant or "",
            item.source_revision or "",
            item.checksum or "",
        ),
    )


def features_at(
    events: Iterable[FeatureEvent], decision_at: datetime, calendar: TradingCalendar
) -> tuple[FeatureEvent, ...]:
    """Return only completed feature bars available by the decision timestamp."""
    if decision_at.tzinfo is None or decision_at.utcoffset() != timedelta(hours=8):
        raise ValueError("decision_at must use Asia/Taipei (+08:00)")
    available = []
    for event in events:
        if event.bar_end > decision_at or event.available_at > decision_at:
            continue
        session = calendar.session_at(event.instrument_id, event.bar_end, event.contract_month)
        start_session = calendar.session_at(
            event.instrument_id, event.bar_start, event.contract_month
        )
        if (
            session is not None
            and start_session == session
            and session.kind == event.session
            and session.trade_date == event.trade_date
        ):
            available.append(event)
    return tuple(
        sorted(
            available,
            key=lambda event: (event.bar_end, event.instrument_id, event.contract_month or ""),
        )
    )


@dataclass(frozen=True)
class QualityIssue:
    row: int
    rule: str
    detail: str


@dataclass(frozen=True)
class QualityAudit:
    raw_count: int
    raw: tuple[Observation, ...]
    clean: tuple[Observation, ...]
    issues: tuple[QualityIssue, ...]


def audit_observations(
    raw: Iterable[Observation],
    calendar: TradingCalendar,
    *,
    interval: timedelta | None = None,
    max_price_change: Decimal | None = None,
    decision_at: datetime | None = None,
) -> QualityAudit:
    """Validate rows and retain raw-to-clean decisions as row-addressed issues."""
    rows = list(raw)
    if decision_at is not None and (
        decision_at.tzinfo is None or decision_at.utcoffset() != timedelta(hours=8)
    ):
        raise ValueError("decision_at must use Asia/Taipei (+08:00)")
    if max_price_change is not None and max_price_change < 0:
        raise ValueError("max_price_change must be non-negative")
    if interval is not None and interval <= timedelta(0):
        raise ValueError("interval must be positive")
    issues: list[QualityIssue] = []
    cutoff = decision_at or max((item.available_at for item in rows), default=None)
    available_rows = [
        (number, item)
        for number, item in enumerate(rows, 1)
        if cutoff is None or item.available_at <= cutoff
    ]

    def logical_key(item: Observation) -> tuple[object, ...]:
        return (
            item.source_id,
            item.instrument_id,
            item.contract_month,
            item.trade_date,
            item.event_at,
            item.session,
            item.participant,
        )

    revisions: dict[tuple[object, ...], list[tuple[int, Observation]]] = defaultdict(list)
    for number, item in available_rows:
        revisions[logical_key(item)].append((number, item))

    selected: list[tuple[int, Observation]] = []
    for key, versions in revisions.items():
        rank = max((item.available_at, item.ingested_at) for _, item in versions)
        latest = [
            (number, item)
            for number, item in versions
            if (item.available_at, item.ingested_at) == rank
        ]
        if any(item.values != latest[0][1].values for _, item in latest[1:]):
            same_revision = len({item.source_revision for _, item in latest}) == 1
            rule = "duplicate_conflict" if same_revision else "revision_conflict"
            for number, _ in latest:
                issues.append(
                    QualityIssue(number, rule, f"conflicting latest versions for event {key}")
                )
            continue
        chosen_item = max((item for _, item in latest), key=_revision_winner_key)
        chosen = next((number, item) for number, item in latest if item is chosen_item)
        selected.append(chosen)
        for number, _ in latest:
            if number != chosen[0]:
                issues.append(
                    QualityIssue(number, "duplicate", f"duplicate of latest row {chosen[0]}")
                )

    excluded: set[int] = set()
    valid_rows: list[tuple[int, Observation]] = []
    for number, item in selected:
        values = item.values
        high, low = values.get("high"), values.get("low")
        if high is not None and low is not None and high < low:
            issues.append(QualityIssue(number, "outlier", "high is below low"))
            excluded.add(number)
            continue
        for field in ("open", "close"):
            price = values.get(field)
            if price is not None and (
                (high is not None and price > high) or (low is not None and price < low)
            ):
                issues.append(QualityIssue(number, "outlier", f"{field} is outside high-low range"))
                excluded.add(number)
                break
        if number in excluded:
            continue
        if any(
            value < 0
            for name, value in values.items()
            if name in {"volume", "open_interest"} and value is not None
        ):
            issues.append(QualityIssue(number, "outlier", "volume or open interest is negative"))
            excluded.add(number)
            continue
        try:
            if item.instrument_id == "TAIEX":
                valid_session = (
                    calendar.is_trade_date(item.trade_date)
                    and item.session == "day"
                    and item.event_at.date() == item.trade_date
                    and item.event_at.time() == calendar.cash_close
                )
            elif item.instrument_id in calendar.contracts and item.session is None:
                session = calendar.day_session(
                    item.instrument_id, item.trade_date, item.contract_month
                )
                valid_session = session is not None and item.event_at == session.closes_at
            elif item.instrument_id in calendar.contracts:
                session = calendar.session_at(
                    item.instrument_id, item.event_at, item.contract_month
                )
                valid_session = (
                    session is not None
                    and session.kind == item.session
                    and session.trade_date == item.trade_date
                )
            else:
                valid_session = False
        except (CalendarGap, ValueError):
            valid_session = False
        if not valid_session:
            issues.append(
                QualityIssue(
                    number,
                    "session_boundary",
                    "event does not match its declared session and trade date",
                )
            )
            excluded.add(number)
            continue
        valid_rows.append((number, item))

    series: dict[
        tuple[str, str, str | None, date, str | None, str | None], list[tuple[int, Observation]]
    ] = defaultdict(list)
    for number, item in valid_rows:
        series[
            (
                item.source_id,
                item.instrument_id,
                item.contract_month,
                item.trade_date,
                item.session,
                item.participant,
            )
        ].append((number, item))

    for entries in series.values():
        ordered = sorted(entries, key=lambda pair: pair[1].event_at)
        previous_valid: tuple[int, Observation] | None = None
        for number, current in ordered:
            if previous_valid is not None:
                previous = previous_valid[1]
                if interval is not None and current.event_at - previous.event_at > interval:
                    issues.append(
                        QualityIssue(
                            number, "gap", f"gap of {current.event_at - previous.event_at}"
                        )
                    )
                old_close, new_close = previous.values.get("close"), current.values.get("close")
                if (
                    max_price_change is not None
                    and old_close is not None
                    and new_close is not None
                    and (
                        old_close == 0
                        or abs(Decimal(new_close) / Decimal(old_close) - 1) > max_price_change
                    )
                ):
                    issues.append(
                        QualityIssue(
                            number, "outlier", "close-to-close change exceeds configured limit"
                        )
                    )
                    excluded.add(number)
                    continue
            previous_valid = (number, current)
    return QualityAudit(
        len(rows),
        tuple(rows),
        tuple(item for number, item in valid_rows if number not in excluded),
        tuple(issues),
    )
