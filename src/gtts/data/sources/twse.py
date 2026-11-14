"""TWSE end-of-day TAIEX index observations."""

from dataclasses import dataclass
from datetime import date, datetime

from pydantic import Field

from gtts.data.calendar import TradingCalendar
from gtts.data.schema import TAIPEI
from gtts.data.sources import DailySource, Observation, Snapshot, SourceRow, observation, price


class IndexRow(SourceRow):
    close: str = Field(alias="TAIEX")
    shares: str = Field(alias="TradeVolume")

    @staticmethod
    def parse_date(value: str) -> date:
        if len(value) != 7 or not value.isascii() or not value.isdigit():
            raise ValueError(f"expected a TWSE ROC date (YYYMMDD): {value!r}")
        return date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:]))


@dataclass(frozen=True)
class TwseIndex(DailySource[IndexRow]):
    endpoint: str = "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK"
    source_id = "twse-index"
    row_type = IndexRow

    def normalize(
        self, row: IndexRow, snapshot: Snapshot, calendar: TradingCalendar
    ) -> Observation:
        shares = int(row.shares.replace(",", ""))
        if shares < 0:
            raise ValueError(f"negative TWSE traded shares: {shares}")
        return observation(
            snapshot,
            calendar,
            source_id=self.source_id,
            instrument_id="TAIEX",
            event_at=datetime.combine(snapshot.trade_date, calendar.cash_close, TAIPEI),
            session="day",
            values={"close": price(row.close), "market_shares_traded": shares},
        )
