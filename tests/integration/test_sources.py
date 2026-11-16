import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from gtts.data.calendar import TradingCalendar
from gtts.data.ingest import ingest_daily
from gtts.data.schema import TAIPEI
from gtts.data.sources.taifex import TaifexFutures, TaifexInstitutional
from gtts.data.sources.twse import TwseIndex

TRADE_DATE = date(2026, 11, 13)
FETCHED = datetime(2026, 11, 13, 18, tzinfo=TAIPEI)
DELAY = datetime(2026, 11, 16, 8, 45, tzinfo=TAIPEI)
ADAPTERS = (TaifexFutures(), TaifexInstitutional(), TwseIndex())


def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


def fixture(source_id: str) -> bytes:
    return (Path(__file__).parents[1] / "fixtures" / "sources" / f"{source_id}.json").read_bytes()


def ingest_row(adapter, row: dict[str, str]):
    return ingest_daily(
        TRADE_DATE,
        calendar(),
        [adapter],
        None,
        fetch=lambda *_: (json.dumps([row]).encode(), None),
        now=lambda: FETCHED,
    )


def test_sources_normalize_offline_and_reuse_cached_raw(tmp_path: Path) -> None:
    database = tmp_path / "ingest.sqlite"
    payloads = {adapter.endpoint: fixture(adapter.source_id) for adapter in ADAPTERS}
    fetch = Mock(side_effect=lambda endpoint, _: (payloads[endpoint], "fixture-v1"))
    result = ingest_daily(
        TRADE_DATE, calendar(), ADAPTERS, database, fetch=fetch, now=lambda: FETCHED
    )
    assert len(result.records) == 11
    assert result.gaps == []
    assert fetch.call_count == 3
    for record in result.records:
        assert record.trade_date == TRADE_DATE
        assert record.timezone == "Asia/Taipei"
        assert record.ingested_at == FETCHED
        assert record.published_at is None
        assert record.availability_estimated
        assert record.available_at == DELAY
        assert record.source_revision == "fixture-v1"
    night = next(record for record in result.records if record.session == "night")
    assert night.contract_month == "202611"
    assert night.event_at == datetime(2026, 11, 13, 5, tzinfo=TAIPEI)
    assert night.checksum == sha256(fixture("taifex-futures")).hexdigest()
    assert night.values["settlement"] is None
    institutional = next(record for record in result.records if record.participant == "自營商")
    assert institutional.contract_month is None
    assert institutional.values["open_interest_net"] == -23
    index = next(record for record in result.records if record.instrument_id == "TAIEX")
    assert index.values["close"] == Decimal("22015.50")
    assert index.event_at.hour == 13 and index.event_at.minute == 30

    cached = ingest_daily(TRADE_DATE, calendar(), ADAPTERS, database, fetch=fetch)
    assert cached == result
    assert fetch.call_count == 3


def test_refresh_preserves_revisions_and_first_seen_timestamp(tmp_path: Path) -> None:
    database = tmp_path / "ingest.sqlite"
    adapter = TwseIndex()
    raw = fixture(adapter.source_id)
    corrected = raw.replace(b"22015.50", b"22111.25")
    fetch = Mock(side_effect=[(raw, None), (raw, None), (corrected, "v2"), (raw, None)])
    later = datetime(2026, 11, 17, 8, tzinfo=TAIPEI)
    first = ingest_daily(
        TRADE_DATE, calendar(), [adapter], database, fetch=fetch, now=lambda: FETCHED
    )
    duplicate = ingest_daily(
        TRADE_DATE, calendar(), [adapter], database, fetch=fetch, now=lambda: later, refresh=True
    )
    assert duplicate == first
    assert first.records[0].source_revision == f"sha256:{first.records[0].checksum}"
    revised = ingest_daily(
        TRADE_DATE, calendar(), [adapter], database, fetch=fetch, now=lambda: later, refresh=True
    )
    assert revised.records[0].available_at == later
    assert revised.records[0].source_revision == "v2"
    restored = ingest_daily(
        TRADE_DATE, calendar(), [adapter], database, fetch=fetch, now=lambda: later, refresh=True
    )
    assert restored == first
    assert ingest_daily(TRADE_DATE, calendar(), [adapter], database, fetch=fetch) == first
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


@pytest.mark.parametrize("bad_payload, kind", [(b"[]", "missing"), (b"<html>", "invalid")])
def test_refresh_with_gaps_keeps_complete_cached_snapshot(
    tmp_path: Path, bad_payload: bytes, kind: str
) -> None:
    database = tmp_path / "ingest.sqlite"
    adapter = TwseIndex()
    fetch = Mock(side_effect=[(fixture(adapter.source_id), None), (bad_payload, None)])
    first = ingest_daily(
        TRADE_DATE, calendar(), [adapter], database, fetch=fetch, now=lambda: FETCHED
    )
    failed = ingest_daily(TRADE_DATE, calendar(), [adapter], database, fetch=fetch, refresh=True)
    assert failed.records == []
    assert [gap.kind for gap in failed.gaps] == [kind]
    assert ingest_daily(TRADE_DATE, calendar(), [adapter], database, fetch=fetch) == first
    assert fetch.call_count == 2
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        with db:
            # Older databases could point at a response that contained gaps.
            db.execute("UPDATE current_snapshots SET checksum=?", (sha256(bad_payload).hexdigest(),))
    assert ingest_daily(TRADE_DATE, calendar(), [adapter], database, fetch=fetch) == first
    assert fetch.call_count == 2


def test_timeout_retries_and_nontransient_http_error_does_not() -> None:
    fetch = Mock(side_effect=[TimeoutError(), (fixture("twse-index"), None)])
    result = ingest_daily(
        TRADE_DATE, calendar(), [TwseIndex()], None, fetch=fetch, timeout=0.5, now=lambda: FETCHED
    )
    assert result.records and fetch.call_count == 2
    assert fetch.call_args.args[1] == 0.5
    denied = Mock(side_effect=HTTPError("https://unused.test", 403, "denied", {}, None))
    result = ingest_daily(TRADE_DATE, calendar(), [TwseIndex()], None, fetch=denied)
    assert denied.call_count == 1
    assert result.gaps[0].kind == "fetch" and result.gaps[0].detail == "HTTP 403"


def test_conflicts_and_missing_rows_are_visible() -> None:
    entries = json.loads(fixture("twse-index"))
    entries.append(entries[-1] | {"TAIEX": "100.25"})
    result = ingest_daily(
        TRADE_DATE,
        calendar(),
        [TwseIndex()],
        None,
        fetch=lambda *_: (json.dumps(entries).encode(), None),
        now=lambda: FETCHED,
    )
    assert result.records == []
    assert [gap.kind for gap in result.gaps] == ["invalid", "missing"]


def test_off_scope_date_and_corrupted_cache_are_visible(tmp_path: Path) -> None:
    database = tmp_path / "ingest.sqlite"
    fetch = Mock(return_value=(fixture("twse-index"), None))
    outside = ingest_daily(date(2025, 11, 13), calendar(), [TwseIndex()], database, fetch=fetch)
    assert outside.gaps[0].kind == "calendar"
    assert fetch.call_count == 0
    ingest_daily(TRADE_DATE, calendar(), [TwseIndex()], database, fetch=fetch, now=lambda: FETCHED)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE snapshots SET payload=?", (b"tampered",))
    invalid = ingest_daily(TRADE_DATE, calendar(), [TwseIndex()], database, fetch=fetch)
    assert invalid.records == []
    assert invalid.gaps[0].kind == "cache"
    assert fetch.call_count == 1
    repaired = ingest_daily(
        TRADE_DATE,
        calendar(),
        [TwseIndex()],
        database,
        fetch=fetch,
        now=lambda: FETCHED,
        refresh=True,
    )
    assert repaired.records and repaired.gaps == []


@pytest.mark.parametrize("timeout, attempts", [(0, 1), (-1, 1), (float("inf"), 1), (1, 0)])
def test_ingest_rejects_invalid_retry_settings(timeout: float, attempts: int) -> None:
    with pytest.raises(ValueError, match="timeout and attempts must be positive"):
        ingest_daily(TRADE_DATE, calendar(), [TwseIndex()], None, timeout=timeout, attempts=attempts)


def test_non_trade_date_skips_fetch() -> None:
    fetch = Mock()
    result = ingest_daily(date(2026, 11, 14), calendar(), [TwseIndex()], None, fetch=fetch)
    assert result.records == result.gaps == []
    fetch.assert_not_called()


def test_transient_http_errors_retry_then_report_fetch_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr("gtts.data.ingest.sleep", waits.append)
    fetch = Mock(side_effect=HTTPError("https://unused.test", 503, "busy", {}, None))
    result = ingest_daily(TRADE_DATE, calendar(), [TwseIndex()], None, fetch=fetch, attempts=2)
    assert fetch.call_count == 2
    assert waits == [0.25]
    assert [(gap.kind, gap.detail) for gap in result.gaps] == [("fetch", "HTTP 503")]


def test_ingest_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ingest_daily(
            TRADE_DATE,
            calendar(),
            [TwseIndex()],
            None,
            fetch=lambda *_: (fixture("twse-index"), None),
            now=lambda: datetime(2026, 11, 13, 18),
        )


@pytest.mark.parametrize(
    "changes, kind, detail",
    [
        ({"Contract": "UNKNOWN"}, "missing", None),
        ({"ContractMonth(Week)": "202611/202612"}, "missing", None),
        ({"TradingSession": "未知"}, "invalid", "unknown TAIFEX session"),
        ({"Volume": "-1"}, "invalid", "negative count"),
    ],
)
def test_taifex_futures_skips_or_rejects_unsupported_rows(
    changes: dict[str, str], kind: str, detail: str | None
) -> None:
    row = next(
        row for row in json.loads(fixture("taifex-futures")) if row["Date"] == "20261113"
    ) | changes
    result = ingest_row(TaifexFutures(), row)
    assert result.records == []
    gaps = [gap for gap in result.gaps if gap.kind == kind]
    assert gaps
    if detail is not None:
        assert detail in gaps[0].detail


def test_taifex_futures_rejects_expired_contract_and_empty_market_values() -> None:
    source_row = next(
        row for row in json.loads(fixture("taifex-futures")) if row["Date"] == "20261113"
    )
    expired = ingest_row(
        TaifexFutures(), source_row | {"ContractMonth(Week)": "202610"}
    )
    assert any("no day session" in gap.detail for gap in expired.gaps)

    empty_values = source_row | {
        "Open": "-",
        "High": "-",
        "Low": "-",
        "Last": "-",
        "Volume": "-",
        "SettlementPrice": "-",
        "OpenInterest": "-",
    }
    empty = ingest_row(TaifexFutures(), empty_values)
    assert any("no market values" in gap.detail for gap in empty.gaps)


def test_taifex_missing_rows_omits_expiry_night_sessions() -> None:
    missing = TaifexFutures().missing([], date(2026, 11, 14), calendar())

    assert len(missing) == 2
    assert all("night" not in item for item in missing)


@pytest.mark.parametrize(
    "changes, kind, detail",
    [
        ({"ContractCode": "UNKNOWN"}, "missing", None),
        ({"Item": "未知法人"}, "invalid", "unknown TAIFEX participant"),
    ],
)
def test_taifex_institutional_skips_or_rejects_unsupported_rows(
    changes: dict[str, str], kind: str, detail: str | None
) -> None:
    row = next(
        row
        for row in json.loads(fixture("taifex-institutional"))
        if row["Date"] == "20261113"
    ) | changes
    result = ingest_row(TaifexInstitutional(), row)
    assert result.records == []
    gaps = [gap for gap in result.gaps if gap.kind == kind]
    assert gaps
    if detail is not None:
        assert detail in gaps[0].detail


@pytest.mark.parametrize(
    "changes, detail",
    [
        ({"Date": "1151332"}, "month must be in 1..12"),
        ({"TradeVolume": "-1"}, "negative TWSE traded shares"),
    ],
)
def test_twse_rejects_invalid_roc_date_or_negative_volume(
    changes: dict[str, str], detail: str
) -> None:
    row = next(
        row for row in json.loads(fixture("twse-index")) if row["Date"] == "1151113"
    ) | changes
    result = ingest_row(TwseIndex(), row)
    assert result.records == []
    assert any(gap.kind == "invalid" and detail in gap.detail for gap in result.gaps)


def test_twse_rejects_non_roc_date_format() -> None:
    row = next(
        row for row in json.loads(fixture("twse-index")) if row["Date"] == "1151113"
    ) | {"Date": "11511AB"}
    result = ingest_row(TwseIndex(), row)
    assert result.records == []
    assert any("expected a TWSE ROC date" in gap.detail for gap in result.gaps)
