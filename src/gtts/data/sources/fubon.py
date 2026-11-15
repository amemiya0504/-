"""Raw five-minute futures candles from an injected Fubon historical client."""

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from gtts.data.calendar import TradingCalendar
from gtts.data.schema import Schema

PRODUCTS = {"TXF": "TX", "MXF": "MTX"}
SESSIONS = {"regular": "REGULAR", "afterhours": "AFTERHOURS"}


class HistoricalCandlesClient(Protocol):
    def candles(self, **params: str) -> object: ...


class FubonCandle(Schema):
    """Auditable raw bar; availability means first retrieval, not historical publication."""

    source_id: Literal["fubon-futures-historical-candles"] = "fubon-futures-historical-candles"
    product: Literal["TXF", "MXF"]
    instrument_id: Literal["TX", "MTX"]
    contract_month: str = Field(pattern=r"^\d{6}$")
    trade_date: date
    session: Literal["regular", "afterhours"]
    raw_timestamp: str
    bar_start: datetime
    bar_end: datetime
    ingested_at: datetime
    available_at: datetime
    availability_estimated: Literal[True] = True
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int = Field(ge=0, strict=True)
    source_revision: None = None
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: dict[str, str]

    @field_validator("bar_start", "bar_end", "ingested_at", "available_at")
    @classmethod
    def timestamps_use_taipei(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(hours=8):
            raise ValueError("timestamps must use Asia/Taipei (+08:00)")
        return value

    @field_validator("open", "high", "low", "close")
    @classmethod
    def prices_are_finite_positive(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("candle prices must be finite and positive")
        return value

    @model_validator(mode="after")
    def candle_is_consistent(self) -> "FubonCandle":
        if self.instrument_id != PRODUCTS[self.product]:
            raise ValueError("product and instrument_id do not match")
        if self.bar_end - self.bar_start != timedelta(minutes=5):
            raise ValueError("Fubon candle must span five minutes")
        if self.available_at != self.ingested_at or self.ingested_at < self.bar_end:
            raise ValueError("historical candles become available only at or after retrieval")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("high is below low")
        for price in (self.open, self.close):
            if price is not None and (
                (self.high is not None and price > self.high)
                or (self.low is not None and price < self.low)
            ):
                raise ValueError("open or close is outside high-low range")
        return self


def _price(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"invalid Fubon {name}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid Fubon {name}") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"invalid Fubon {name}")
    return result


class FubonFuturesCandles:
    """Read exact-month TXF/MXF 5-minute candles without constructing or logging in an SDK."""

    source_id = "fubon-futures-historical-candles"

    def __init__(self, client: HistoricalCandlesClient, calendar: TradingCalendar) -> None:
        self.client = client
        self.calendar = calendar

    def read(
        self,
        product: str,
        contract_month: str,
        trade_date: date,
        session: str,
        ingested_at: datetime,
    ) -> list[FubonCandle]:
        if product not in PRODUCTS:
            raise ValueError("product must be TXF or MXF")
        if not re.fullmatch(r"\d{6}", contract_month):
            raise ValueError("contract_month must be an explicit YYYYMM month")
        if session not in SESSIONS:
            raise ValueError("session must be regular or afterhours")
        response = self.client.candles(
            **{
                "product": product,
                "contractMonth": contract_month,
                "timeframe": "5",
                "session": session,
                "from": trade_date.isoformat(),
                "to": trade_date.isoformat(),
                "fields": "open,high,low,close,volume",
                "sort": "asc",
            }
        )
        if not isinstance(response, Mapping):
            raise TypeError("Fubon response must be an object")
        if (
            response.get("product") != product
            or response.get("exchange") != "TAIFEX"
            or response.get("contractMonth") != contract_month
            or response.get("timeframe") != "5"
            or response.get("session") != SESSIONS[session]
            or response.get("sort") != "asc"
            or not isinstance(response.get("data"), list)
        ):
            raise ValueError("Fubon response metadata does not match the request")
        if ingested_at.tzinfo is None or ingested_at.utcoffset() != timedelta(hours=8):
            raise ValueError("ingested_at must use Asia/Taipei (+08:00)")

        provenance = {
            "exchange": "TAIFEX",
            "product": product,
            "contractMonth": contract_month,
            "timeframe": "5",
            "session": session,
            "tradeDate": trade_date.isoformat(),
            "sort": "asc",
            "availability_assumption": "first_available_at_retrieval",
        }
        instrument_id = PRODUCTS[product]
        expected_session = (
            self.calendar.day_session(instrument_id, trade_date, contract_month)
            if session == "regular"
            else self.calendar.night_session(instrument_id, trade_date, contract_month)
        )
        if expected_session is None:
            raise ValueError("requested session does not exist for the trade date and contract")
        expected_kind = "day" if session == "regular" else "night"
        candles: list[FubonCandle] = []
        previous: datetime | None = None
        for row in response["data"]:
            if not isinstance(row, Mapping) or row.get("contractMonth") != contract_month:
                raise ValueError("Fubon row has invalid contract month")
            if not {"date", "open", "high", "low", "close", "volume"}.issubset(row):
                raise ValueError("Fubon row is missing a required candle field")
            raw_timestamp = row.get("date")
            if not isinstance(raw_timestamp, str):
                raise TypeError("Fubon row is missing its raw timestamp")
            try:
                bar_start = datetime.fromisoformat(raw_timestamp)
            except ValueError as error:
                raise ValueError("invalid Fubon candle timestamp") from error
            if (
                bar_start.tzinfo is None
                or bar_start.utcoffset() != timedelta(hours=8)
                or bar_start.minute % 5
                or bar_start.second
                or bar_start.microsecond
            ):
                raise ValueError("Fubon timestamp must be a Taipei five-minute boundary")
            if previous is not None and bar_start <= previous:
                raise ValueError("Fubon candles must be unique and ascending")
            previous = bar_start
            bar_end = bar_start + timedelta(minutes=5)
            start_session = self.calendar.session_at(instrument_id, bar_start, contract_month)
            end_session = self.calendar.session_at(instrument_id, bar_end, contract_month)
            if (
                bar_start < expected_session.opens_at
                or bar_end > expected_session.closes_at
                or start_session != expected_session
                or end_session != expected_session
                or expected_session.kind != expected_kind
            ):
                raise ValueError("Fubon candle does not fit requested calendar session/trade_date")
            if not isinstance(row.get("volume"), int) or isinstance(row.get("volume"), bool):
                raise TypeError("Fubon volume must be an integer")
            raw = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            candles.append(
                FubonCandle(
                    product=product,
                    instrument_id=instrument_id,
                    contract_month=contract_month,
                    trade_date=trade_date,
                    session=session,
                    raw_timestamp=raw_timestamp,
                    bar_start=bar_start,
                    bar_end=bar_end,
                    ingested_at=ingested_at,
                    available_at=ingested_at,
                    open=_price(row.get("open"), "open"),
                    high=_price(row.get("high"), "high"),
                    low=_price(row.get("low"), "low"),
                    close=_price(row.get("close"), "close"),
                    volume=row["volume"],
                    checksum=sha256(raw.encode("utf-8")).hexdigest(),
                    provenance=provenance,
                )
            )
        return candles
