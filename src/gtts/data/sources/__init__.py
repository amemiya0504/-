"""Source adapter contract and shared daily observation fields."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Literal, Protocol

from pydantic import ConfigDict, Field

from gtts.data.calendar import CalendarGap, SessionKind, TradingCalendar
from gtts.data.schema import TAIPEI, MarketEvent, Schema


class Observation(MarketEvent):
    availability_estimated: bool
    session: SessionKind | None = None
    participant: str | None = None
    values: dict[str, Decimal | int | None]

    @property
    def key(self) -> tuple[str, str | None, SessionKind | None, str | None]:
        return self.instrument_id, self.contract_month, self.session, self.participant


class Gap(Schema):
    source_id: str
    trade_date: date
    kind: Literal["fetch", "cache", "invalid", "missing", "calendar"]
    detail: str


@dataclass(frozen=True)
class Snapshot:
    trade_date: date
    payload: bytes
    checksum: str
    ingested_at: datetime
    source_revision: str | None = None


class SourceAdapter(Protocol):
    source_id: str
    endpoint: str

    def parse(
        self, snapshot: Snapshot, calendar: TradingCalendar
    ) -> tuple[list[Observation], list[Gap]]: ...


class SourceRow(Schema):
    model_config = ConfigDict(extra="ignore", frozen=True)
    day: str = Field(alias="Date")

    @staticmethod
    def parse_date(value: str) -> date:
        return date.fromisoformat(value)


def rows(payload: bytes) -> list[object]:
    data: object = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(data, list):
        raise TypeError("expected a JSON array of source rows")
    return list(data)


class DailySource[Row: SourceRow]:
    source_id: ClassVar[str]
    row_type: type[Row]

    def parse(
        self, snapshot: Snapshot, calendar: TradingCalendar
    ) -> tuple[list[Observation], list[Gap]]:
        records: dict[tuple[str, str | None, SessionKind | None, str | None], Observation] = {}
        conflicts: set[tuple[str, str | None, SessionKind | None, str | None]] = set()
        gaps: list[Gap] = []
        for number, entry in enumerate(rows(snapshot.payload), start=1):
            try:
                day = self.row_type.parse_date(SourceRow.model_validate(entry).day)
                if day != snapshot.trade_date:
                    continue
                record = self.normalize(self.row_type.model_validate(entry), snapshot, calendar)
                if record is None:
                    continue
                previous = records.get(record.key)
                if previous is not None and previous != record:
                    conflicts.add(record.key)
                    raise ValueError(f"conflicting duplicate {record.key}")
                records[record.key] = record
            except CalendarGap as error:
                gaps.append(
                    Gap(
                        source_id=self.source_id,
                        trade_date=snapshot.trade_date,
                        kind="calendar",
                        detail=f"row {number}: {error}",
                    )
                )
            except ValueError as error:
                gaps.append(
                    Gap(
                        source_id=self.source_id,
                        trade_date=snapshot.trade_date,
                        kind="invalid",
                        detail=f"row {number}: {error}",
                    )
                )
        for key in conflicts:
            records.pop(key, None)
        result = list(records.values())
        try:
            missing = self.missing(result, snapshot.trade_date, calendar)
        except CalendarGap as error:
            gaps.append(
                Gap(
                    source_id=self.source_id,
                    trade_date=snapshot.trade_date,
                    kind="calendar",
                    detail=str(error),
                )
            )
            missing = []
        for detail in missing:
            gaps.append(
                Gap(
                    source_id=self.source_id,
                    trade_date=snapshot.trade_date,
                    kind="missing",
                    detail=detail,
                )
            )
        return result, gaps

    def normalize(
        self, row: Row, snapshot: Snapshot, calendar: TradingCalendar
    ) -> Observation | None:
        raise NotImplementedError

    def missing(
        self, records: list[Observation], trade_date: date, calendar: TradingCalendar
    ) -> list[str]:
        return [] if records else ["no observations for requested trade date"]


def price(value: str) -> Decimal:
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation as error:
        raise ValueError(f"invalid price: {value!r}") from error
    if not number.is_finite() or number <= 0:
        raise ValueError(f"invalid price: {value!r}")
    return number


def observation(
    snapshot: Snapshot,
    calendar: TradingCalendar,
    *,
    source_id: str,
    instrument_id: str,
    event_at: datetime,
    values: dict[str, Decimal | int | None],
    session: SessionKind | None = None,
    contract_month: str | None = None,
    participant: str | None = None,
    published_at: datetime | None = None,
    unknown_publication_delay: timedelta = timedelta(0),
) -> Observation:
    if unknown_publication_delay < timedelta(0):
        raise ValueError("unknown_publication_delay must be non-negative")
    if event_at > snapshot.ingested_at:
        raise ValueError("source row arrived before the event")
    if published_at is not None and not event_at <= published_at <= snapshot.ingested_at:
        raise ValueError("published_at is outside the event-to-ingestion interval")
    available_at = snapshot.ingested_at
    if published_at is None:
        next_day = calendar.next_trade_date(snapshot.trade_date)
        clock = instrument_id if instrument_id in calendar.contracts else "TX"
        open_at = datetime.combine(next_day, calendar.spec(clock, next_day).day_open, TAIPEI)
        available_at = max(open_at + unknown_publication_delay, available_at)
    return Observation(
        source_id=source_id,
        trade_date=snapshot.trade_date,
        instrument_id=instrument_id,
        contract_month=contract_month,
        event_at=event_at,
        published_at=published_at,
        ingested_at=snapshot.ingested_at,
        available_at=available_at,
        source_revision=snapshot.source_revision or f"sha256:{snapshot.checksum}",
        checksum=snapshot.checksum,
        availability_estimated=published_at is None,
        session=session,
        participant=participant,
        values=values,
    )
