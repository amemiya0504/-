"""Fetch, validate and cache daily source snapshots in SQLite."""

import http.client
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import TypeAdapter

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.schema import TAIPEI
from gtts.data.sources import Gap, Observation, Snapshot, SourceAdapter

Fetcher = Callable[[str, float], tuple[bytes, str | None]]

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    source_id TEXT NOT NULL,
    endpoint_key TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    checksum TEXT NOT NULL,
    payload BLOB NOT NULL,
    ingested_at TEXT NOT NULL,
    source_revision TEXT,
    PRIMARY KEY (source_id, endpoint_key, trade_date, checksum)
);
CREATE TABLE IF NOT EXISTS observations (
    source_id TEXT NOT NULL,
    endpoint_key TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    checksum TEXT NOT NULL,
    records_json BLOB NOT NULL,
    gaps_json BLOB NOT NULL,
    PRIMARY KEY (source_id, endpoint_key, trade_date, checksum)
);
CREATE TABLE IF NOT EXISTS current_snapshots (
    source_id TEXT NOT NULL,
    endpoint_key TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    checksum TEXT NOT NULL,
    PRIMARY KEY (source_id, endpoint_key, trade_date)
);
"""

_RECORDS = TypeAdapter(list[Observation])
_GAPS = TypeAdapter(list[Gap])


@dataclass(frozen=True)
class IngestResult:
    records: list[Observation]
    gaps: list[Gap]


def fetch_json(endpoint: str, timeout: float) -> tuple[bytes, str | None]:
    request = Request(endpoint, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("ETag")


def _fetch_with_retry(
    fetch: Fetcher, endpoint: str, timeout: float, attempts: int
) -> tuple[bytes, str | None]:
    for attempt in range(attempts):
        try:
            return fetch(endpoint, timeout)
        except HTTPError as error:
            error.close()
            if error.code not in {408, 429} and not 500 <= error.code < 600:
                raise
            if attempt + 1 == attempts:
                raise
        except (OSError, http.client.HTTPException):
            if attempt + 1 == attempts:
                raise
        sleep(0.25 * 2 ** min(attempt, 4))
    raise AssertionError("attempts must be positive")


def _error_detail(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return f"URL error ({type(error.reason).__name__})"
    return type(error).__name__


def ingest_daily(
    trade_date: date,
    calendar: TradingCalendar,
    adapters: Iterable[SourceAdapter],
    database: Path | None,
    *,
    fetch: Fetcher = fetch_json,
    timeout: float = 15,
    attempts: int = 3,
    refresh: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(TAIPEI),
) -> IngestResult:
    """Reuse the last complete snapshot per endpoint/date unless refresh is requested."""
    if not isfinite(timeout) or timeout <= 0 or attempts < 1:
        raise ValueError("timeout and attempts must be positive")
    try:
        if not calendar.is_trade_date(trade_date):
            return IngestResult([], [])
    except CalendarGap as error:
        return IngestResult(
            [],
            [Gap(source_id="calendar", trade_date=trade_date, kind="calendar", detail=str(error))],
        )

    records: list[Observation] = []
    gaps: list[Gap] = []
    if database is not None:
        database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database if database is not None else ":memory:")) as db:
        db.executescript(_DB_SCHEMA)
        for adapter in adapters:
            result = _ingest_source(
                db, adapter, trade_date, calendar, fetch, timeout, attempts, refresh, now
            )
            records.extend(result.records)
            gaps.extend(result.gaps)
    return IngestResult(records, gaps)


def _snapshot_from_row(trade_date: date, row: tuple[str, bytes, str, str | None]) -> Snapshot:
    checksum, payload, ingested_at, revision = row
    if sha256(payload).hexdigest() != checksum:
        raise ValueError("cached payload SHA-256 mismatch")
    stamp = datetime.fromisoformat(ingested_at)
    if stamp.utcoffset() is None:
        raise ValueError("cached ingested_at has no timezone")
    return Snapshot(trade_date, payload, checksum, stamp.astimezone(TAIPEI), revision)


def _ingest_source(
    db: sqlite3.Connection,
    adapter: SourceAdapter,
    trade_date: date,
    calendar: TradingCalendar,
    fetch: Fetcher,
    timeout: float,
    attempts: int,
    refresh: bool,
    now: Callable[[], datetime],
) -> IngestResult:
    endpoint_key = sha256(adapter.endpoint.encode()).hexdigest()
    key = (adapter.source_id, endpoint_key, trade_date.isoformat())
    row = db.execute(
        "SELECT checksum, payload, ingested_at, source_revision "
        "FROM snapshots JOIN observations USING (source_id, endpoint_key, trade_date, checksum) "
        "LEFT JOIN current_snapshots USING (source_id, endpoint_key, trade_date, checksum) "
        "WHERE snapshots.source_id=? AND snapshots.endpoint_key=? AND snapshots.trade_date=? "
        "AND gaps_json=? ORDER BY current_snapshots.checksum IS NOT NULL DESC, ingested_at DESC "
        "LIMIT 1",
        (*key, b"[]"),
    ).fetchone()
    fetched = row is None or refresh
    if fetched:
        try:
            payload, revision = _fetch_with_retry(fetch, adapter.endpoint, timeout, attempts)
        except (OSError, http.client.HTTPException, ValueError) as error:
            return IngestResult(
                [],
                [
                    Gap(
                        source_id=adapter.source_id,
                        trade_date=trade_date,
                        kind="fetch",
                        detail=_error_detail(error),
                    )
                ],
            )
        checksum = sha256(payload).hexdigest()
        ingested_at = now()
        if ingested_at.utcoffset() is None:
            raise ValueError("ingest clock must return a timezone-aware datetime")

    with db:
        if fetched:
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_id, endpoint_key, trade_date, checksum) DO UPDATE SET "
                "payload=excluded.payload WHERE payload != excluded.payload",
                (*key, checksum, payload, ingested_at.astimezone(TAIPEI).isoformat(), revision),
            )
            row = db.execute(
                "SELECT checksum, payload, ingested_at, source_revision FROM snapshots "
                "WHERE source_id=? AND endpoint_key=? AND trade_date=? AND checksum=?",
                (*key, checksum),
            ).fetchone()
        try:
            snapshot = _snapshot_from_row(trade_date, row)
        except (TypeError, ValueError) as error:
            return IngestResult(
                [],
                [
                    Gap(
                        source_id=adapter.source_id,
                        trade_date=trade_date,
                        kind="cache",
                        detail=str(error),
                    )
                ],
            )
        try:
            records, gaps = adapter.parse(snapshot, calendar)
        except CalendarGap as error:
            records = []
            gaps = [
                Gap(
                    source_id=adapter.source_id,
                    trade_date=trade_date,
                    kind="calendar",
                    detail=str(error),
                )
            ]
        except (TypeError, ValueError) as error:
            records = []
            gaps = [
                Gap(
                    source_id=adapter.source_id,
                    trade_date=trade_date,
                    kind="invalid",
                    detail=str(error),
                )
            ]
        db.execute(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source_id, endpoint_key, trade_date, checksum) DO UPDATE SET "
            "records_json=excluded.records_json, gaps_json=excluded.gaps_json",
            (*key, snapshot.checksum, _RECORDS.dump_json(records), _GAPS.dump_json(gaps)),
        )
        if fetched and not gaps:
            db.execute(
                "INSERT INTO current_snapshots VALUES (?, ?, ?, ?) "
                "ON CONFLICT (source_id, endpoint_key, trade_date) DO UPDATE SET "
                "checksum=excluded.checksum",
                (*key, snapshot.checksum),
            )
    return IngestResult(records, gaps)
