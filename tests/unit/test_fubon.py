import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from gtts.data.calendar import TradingCalendar
from gtts.data.schema import TAIPEI
from gtts.data.sources.fubon import FubonFuturesCandles

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
