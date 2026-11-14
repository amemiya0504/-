"""Minimal event schemas for the offline GTTS research scaffold."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TAIPEI = ZoneInfo("Asia/Taipei")


def _taipei_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(hours=8):
        raise ValueError("datetime must be timezone-aware and use Asia/Taipei (+08:00)")
    return value


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketEvent(Schema):
    source_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    contract_month: str | None = None
    trade_date: date
    event_at: datetime
    published_at: datetime | None = None
    ingested_at: datetime
    available_at: datetime
    timezone: Literal["Asia/Taipei"] = "Asia/Taipei"
    source_revision: str | None = None
    checksum: str | None = None

    @field_validator("event_at", "published_at", "ingested_at", "available_at")
    @classmethod
    def timestamps_use_taipei(cls, value: datetime | None) -> datetime | None:
        return _taipei_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def timestamps_are_in_order(self) -> "MarketEvent":
        if self.available_at < self.event_at:
            raise ValueError("available_at must be at or after event_at")
        if self.ingested_at < self.event_at:
            raise ValueError("ingested_at must be at or after event_at")
        if self.published_at is None:
            if self.available_at < self.ingested_at:
                raise ValueError("available_at must be at or after ingested_at without published_at")
        else:
            if not self.event_at <= self.published_at <= self.ingested_at:
                raise ValueError("published_at must be between event_at and ingested_at")
            if self.available_at < self.published_at:
                raise ValueError("available_at must be at or after published_at")
        return self


class SignalEvent(Schema):
    signal_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    decision_at: datetime
    available_at: datetime
    probability: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("decision_at", "available_at")
    @classmethod
    def timestamps_use_taipei(cls, value: datetime) -> datetime:
        return _taipei_datetime(value)

    @model_validator(mode="after")
    def data_is_available_at_decision(self) -> "SignalEvent":
        if self.available_at > self.decision_at:
            raise ValueError("signal data must be available by decision_at")
        return self


class OrderEvent(Schema):
    order_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    order_at: datetime

    @field_validator("order_at")
    @classmethod
    def timestamp_uses_taipei(cls, value: datetime) -> datetime:
        return _taipei_datetime(value)


class FillEvent(Schema):
    fill_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    signal_at: datetime
    fill_at: datetime

    @field_validator("signal_at", "fill_at")
    @classmethod
    def timestamps_use_taipei(cls, value: datetime) -> datetime:
        return _taipei_datetime(value)

    @model_validator(mode="after")
    def fill_follows_signal(self) -> "FillEvent":
        if self.fill_at <= self.signal_at:
            raise ValueError("fill_at must be later than signal_at")
        return self


class PortfolioSnapshot(Schema):
    snapshot_at: datetime
    cash: Decimal
    equity: Decimal
    margin_used: Decimal = Field(ge=0)
    positions: dict[str, int]

    @field_validator("snapshot_at")
    @classmethod
    def timestamp_uses_taipei(cls, value: datetime) -> datetime:
        return _taipei_datetime(value)
