import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from gtts.data.calendar import TradingCalendar
from gtts.data.schema import TAIPEI
from gtts.data.sources.fubon import FubonCandle, FubonFuturesCandles, save_jsonl

TRADE_DATE = date(2026, 11, 13)
RETRIEVED = datetime(2026, 11, 16, 8, 45, tzinfo=TAIPEI)


def fixture() -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures" / "sources" / "fubon-historical-candles.json"
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def calendar() -> TradingCalendar:
    path = Path(__file__).parents[2] / "configs" / "contracts.yaml"
    return TradingCalendar.from_yaml(path)


def test_read_preserves_raw_candle_and_retrieval_availability() -> None:
    client = Mock(candles=Mock(return_value=fixture()))
    candles = FubonFuturesCandles(client, calendar()).read(
        "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
    )

    assert [item.raw_timestamp for item in candles] == [
        "2026-11-13T09:00:00.000+08:00",
        "2026-11-13T09:05:00.000+08:00",
    ]
    assert candles[0].instrument_id == "TX"
    assert candles[0].session == "regular"
    assert candles[0].trade_date == TRADE_DATE
    assert candles[0].bar_end == datetime(2026, 11, 13, 9, 5, tzinfo=TAIPEI)
    assert candles[0].available_at == RETRIEVED
    assert candles[0].availability_estimated
    assert candles[0].open == Decimal(27500) and candles[0].volume == 123
    assert candles[0].checksum != candles[1].checksum
    assert candles[0].provenance["availability_assumption"] == "first_available_at_retrieval"
    client.candles.assert_called_once_with(
        product="TXF",
        contractMonth="202611",
        timeframe="5",
        session="regular",
        **{"from": "2026-11-13", "to": "2026-11-13"},
        fields="open,high,low,close,volume",
        sort="asc",
    )


@pytest.mark.parametrize(
    ("product", "contract_month", "session", "message"),
    [
        ("TX", "202611", "regular", "product must be TXF or MXF"),
        ("TXF", "1!", "regular", "explicit YYYYMM"),
        ("TXF", "202611W1", "regular", "explicit YYYYMM"),
        ("TXF", "202611", "night", "regular or afterhours"),
    ],
)
def test_rejects_out_of_scope_requests(
    product: str, contract_month: str, session: str, message: str
) -> None:
    client = Mock()
    with pytest.raises(ValueError, match=message):
        FubonFuturesCandles(client, calendar()).read(
            product, contract_month, TRADE_DATE, session, RETRIEVED
        )
    client.candles.assert_not_called()


def test_rejects_response_metadata_or_contract_mismatch() -> None:
    response = fixture()
    response["contractMonth"] = "1!"
    client = Mock(candles=Mock(return_value=response))
    with pytest.raises(ValueError, match="metadata does not match"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
        )

    response = fixture()
    rows = response["data"]
    assert isinstance(rows, list)
    rows[0]["contractMonth"] = "202612"
    client.candles.return_value = response
    with pytest.raises(ValueError, match="invalid contract month"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
        )


def test_rejects_unexpected_exchange() -> None:
    response = fixture()
    response["exchange"] = "NA"
    client = Mock(candles=Mock(return_value=response))

    with pytest.raises(ValueError, match="metadata does not match"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
        )


def test_rejects_invalid_calendar_or_ingest_time_before_api_call() -> None:
    client = Mock()
    source = FubonFuturesCandles(client, calendar())

    with pytest.raises(ValueError, match="requested session does not exist"):
        source.read("TXF", "202611", date(2026, 11, 15), "regular", RETRIEVED)
    with pytest.raises(ValueError, match="Asia/Taipei"):
        source.read("TXF", "202611", TRADE_DATE, "regular", datetime(2026, 11, 13, 9))

    client.candles.assert_not_called()


@pytest.mark.parametrize(
    ("session", "trade_date", "timestamp"),
    [
        ("regular", TRADE_DATE, "2026-11-12T09:00:00.000+08:00"),
        ("regular", TRADE_DATE, "2026-11-13T15:00:00.000+08:00"),
        ("afterhours", date(2026, 11, 16), "2026-11-15T15:00:00.000+08:00"),
    ],
)
def test_rejects_candles_outside_requested_session(
    session: str, trade_date: date, timestamp: str
) -> None:
    response = fixture()
    response["session"] = "REGULAR" if session == "regular" else "AFTERHOURS"
    rows = response["data"]
    assert isinstance(rows, list)
    rows[0]["date"] = timestamp
    client = Mock(candles=Mock(return_value=response))

    with pytest.raises(ValueError, match="calendar session/trade_date"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", trade_date, session, RETRIEVED
        )


def test_reads_mxf_afterhours_with_exchange_trade_date() -> None:
    response = fixture()
    response.update(product="MXF", contractMonth="202611", session="AFTERHOURS")
    rows = response["data"]
    assert isinstance(rows, list)
    rows[:] = [
        {
            "date": "2026-11-12T15:00:00.000+08:00",
            "contractMonth": "202611",
            "open": 27500,
            "high": 27510,
            "low": 27495,
            "close": 27505,
            "volume": 20,
        }
    ]
    client = Mock(candles=Mock(return_value=response))

    candle = FubonFuturesCandles(client, calendar()).read(
        "MXF", "202611", TRADE_DATE, "afterhours", RETRIEVED
    )[0]

    assert candle.instrument_id == "MTX"
    assert candle.trade_date == TRADE_DATE
    assert candle.bar_start.date() == date(2026, 11, 12)
    assert candle.session == "afterhours"
    assert candle.provenance["tradeDate"] == "2026-11-13"


def test_null_fubon_price_remains_missing() -> None:
    response = fixture()
    rows = response["data"]
    assert isinstance(rows, list)
    rows[0]["open"] = None
    client = Mock(candles=Mock(return_value=response))

    candles = FubonFuturesCandles(client, calendar()).read(
        "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
    )

    assert candles[0].open is None


def test_defaults_availability_to_retrieval_time_after_api_response() -> None:
    client = Mock(candles=Mock(return_value=fixture()))

    candles = FubonFuturesCandles(client, calendar()).read("TXF", "202611", TRADE_DATE, "regular")

    assert candles
    assert candles[0].available_at >= candles[0].bar_end
    assert candles[0].available_at <= datetime.now(TAIPEI)


def test_save_jsonl_preserves_candles_and_never_overwrites(tmp_path: Path) -> None:
    client = Mock(candles=Mock(return_value=fixture()))
    candles = FubonFuturesCandles(client, calendar()).read(
        "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
    )
    output = tmp_path / "bars.jsonl"

    save_jsonl(output, candles)

    assert len(output.read_text(encoding="utf-8").splitlines()) == len(candles)
    with pytest.raises(FileExistsError):
        save_jsonl(output, candles)


def test_rejects_bad_timestamps_duplicates_and_early_retrieval() -> None:
    response = fixture()
    rows = response["data"]
    assert isinstance(rows, list)
    rows[1]["date"] = rows[0]["date"]
    client = Mock(candles=Mock(return_value=response))
    with pytest.raises(ValueError, match="unique and ascending"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
        )

    client.candles.return_value = fixture()
    before_close = datetime(2026, 11, 13, 9, 4, tzinfo=TAIPEI)
    with pytest.raises(ValueError, match="at or after retrieval"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", before_close
        )


@pytest.mark.parametrize(
    ("mutate", "error", "message"),
    [
        (lambda row: row.__setitem__("volume", True), TypeError, "volume must be an integer"),
        (lambda row: row.__setitem__("open", True), TypeError, "invalid Fubon open"),
        (lambda row: row.__setitem__("high", 0), ValueError, "invalid Fubon high"),
        (lambda row: row.__setitem__("date", 123), TypeError, "missing its raw timestamp"),
        (lambda row: row.__setitem__("date", "invalid"), ValueError, "invalid Fubon candle timestamp"),
        (lambda row: row.__setitem__("date", "2026-11-13T09:01:00+08:00"), ValueError, "five-minute boundary"),
        (lambda row: row.pop("volume"), ValueError, "missing a required candle field"),
    ],
)
def test_rejects_invalid_fubon_rows(mutate, error: type[Exception], message: str) -> None:
    response = fixture()
    rows = response["data"]
    assert isinstance(rows, list)
    mutate(rows[0])
    client = Mock(candles=Mock(return_value=response))

    with pytest.raises(error, match=message):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"instrument_id": "MTX"}, "product and instrument_id do not match"),
        ({"bar_end": datetime(2026, 11, 13, 9, 10, tzinfo=TAIPEI)}, "span five minutes"),
        ({"available_at": datetime(2026, 11, 16, 8, 46, tzinfo=TAIPEI)}, "at or after retrieval"),
        ({"high": Decimal(27400)}, "high is below low"),
        ({"open": Decimal(27600)}, "outside high-low range"),
        ({"bar_start": datetime(2026, 11, 13, 1, tzinfo=UTC)}, "Asia/Taipei"),
        ({"close": Decimal(0)}, "finite and positive"),
    ],
)
def test_candle_model_rejects_invalid_evidence(changes: dict[str, object], message: str) -> None:
    row = FubonFuturesCandles(Mock(candles=Mock(return_value=fixture())), calendar()).read(
        "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
    )[0]

    with pytest.raises(ValueError, match=message):
        FubonCandle.model_validate(row.model_dump() | changes)


def test_rejects_non_object_fubon_response() -> None:
    client = Mock(candles=Mock(return_value=[]))

    with pytest.raises(TypeError, match="response must be an object"):
        FubonFuturesCandles(client, calendar()).read(
            "TXF", "202611", TRADE_DATE, "regular", RETRIEVED
        )


def test_save_jsonl_removes_partial_file_after_serialization_failure(tmp_path: Path) -> None:
    output = tmp_path / "partial.jsonl"

    with pytest.raises(AttributeError):
        save_jsonl(output, [cast(FubonCandle, object())])

    assert not output.exists()

