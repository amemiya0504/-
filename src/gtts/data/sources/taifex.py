"""TAIFEX daily futures reports and product-level institutional positions."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import Field

from gtts.data.calendar import SessionKind, TradingCalendar
from gtts.data.schema import TAIPEI
from gtts.data.sources import DailySource, Observation, Snapshot, SourceRow, observation, price

PRODUCT_CODES = {"臺股期貨": "TX", "小型臺指期貨": "MTX"}
PARTICIPANTS = ("自營商", "投信", "外資及陸資")
SESSIONS: dict[str, SessionKind] = {"一般": "day", "盤後": "night"}
PRODUCTS = ("TX", "MTX")


def optional_price(value: str) -> Decimal | None:
    return None if value in {"", "-", "NULL"} else price(value)


def optional_count(value: str) -> int | None:
    if value in {"", "-", "NULL"}:
        return None
    number = int(value.replace(",", ""))
    if number < 0:
        raise ValueError(f"negative count: {value!r}")
    return number


class FuturesRow(SourceRow):
    instrument: str = Field(alias="Contract")
    month: str = Field(alias="ContractMonth(Week)")
    trading_session: str = Field(alias="TradingSession")
    open_price: str = Field(alias="Open")
    high: str = Field(alias="High")
    low: str = Field(alias="Low")
    last: str = Field(alias="Last")
    volume: str = Field(alias="Volume")
    settlement: str = Field(alias="SettlementPrice")
    open_interest: str = Field(alias="OpenInterest")


@dataclass(frozen=True)
class TaifexFutures(DailySource[FuturesRow]):
    endpoint: str = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
    source_id = "taifex-futures"
    row_type = FuturesRow

    def normalize(
        self, row: FuturesRow, snapshot: Snapshot, calendar: TradingCalendar
    ) -> Observation | None:
        if row.instrument not in PRODUCTS or "/" in row.month:
            return None
        kind = SESSIONS.get(row.trading_session)
        if kind is None:
            raise ValueError(f"unknown TAIFEX session: {row.trading_session!r}")
        trade_date = snapshot.trade_date
        session = (
            calendar.day_session(row.instrument, trade_date, row.month)
            if kind == "day"
            else calendar.night_session(row.instrument, trade_date, row.month)
        )
        if session is None:
            raise ValueError(f"no {kind} session for {row.instrument} {row.month}")
        values = {
            "open": optional_price(row.open_price),
            "high": optional_price(row.high),
            "low": optional_price(row.low),
            "close": optional_price(row.last),
            "volume": optional_count(row.volume),
            "settlement": optional_price(row.settlement),
            "open_interest": optional_count(row.open_interest),
        }
        if all(value is None for value in values.values()):
            raise ValueError(f"no market values for {row.instrument} {row.month} {kind}")
        return observation(
            snapshot,
            calendar,
            source_id=self.source_id,
            instrument_id=row.instrument,
            contract_month=row.month,
            event_at=session.closes_at,
            session=kind,
            values=values,
        )

    def missing(
        self, records: list[Observation], trade_date: date, calendar: TradingCalendar
    ) -> list[str]:
        expected: list[tuple[str, SessionKind]] = [(instrument, "day") for instrument in PRODUCTS]
        for instrument in PRODUCTS:
            if calendar.night_session(instrument, trade_date):
                expected.append((instrument, "night"))
        actual = {(record.instrument_id, record.session) for record in records}
        return [
            f"missing {instrument} {kind} rows"
            for instrument, kind in expected
            if (instrument, kind) not in actual
        ]


class InstitutionalRow(SourceRow):
    product: str = Field(alias="ContractCode")
    participant: str = Field(alias="Item")
    trading_net: str = Field(alias="TradingVolume(Net)")
    interest_net: str = Field(alias="OpenInterest(Net)")


@dataclass(frozen=True)
class TaifexInstitutional(DailySource[InstitutionalRow]):
    endpoint: str = (
        "https://openapi.taifex.com.tw/v1/"
        "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    )
    source_id = "taifex-institutional"
    row_type = InstitutionalRow

    def normalize(
        self, row: InstitutionalRow, snapshot: Snapshot, calendar: TradingCalendar
    ) -> Observation | None:
        instrument = PRODUCT_CODES.get(row.product)
        if instrument is None:
            return None
        if row.participant not in PARTICIPANTS:
            raise ValueError(f"unknown TAIFEX participant: {row.participant!r}")
        close = calendar.spec(instrument, snapshot.trade_date).day_close
        return observation(
            snapshot,
            calendar,
            source_id=self.source_id,
            instrument_id=instrument,
            participant=row.participant,
            event_at=datetime.combine(snapshot.trade_date, close, TAIPEI),
            values={
                "trading_volume_net": int(row.trading_net.replace(",", "")),
                "open_interest_net": int(row.interest_net.replace(",", "")),
            },
        )

    def missing(
        self, records: list[Observation], trade_date: date, calendar: TradingCalendar
    ) -> list[str]:
        actual = {(record.instrument_id, record.participant) for record in records}
        return [
            f"missing {instrument} {participant} row"
            for instrument in PRODUCTS
            for participant in PARTICIPANTS
            if (instrument, participant) not in actual
        ]
