"""Effective-dated contract terms and Taiwan futures trading sessions."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, model_validator

from gtts.data.schema import TAIPEI, Schema

SessionKind = Literal["day", "night"]
ONE_DAY = timedelta(days=1)


class CalendarGap(ValueError):
    """The requested date is outside verified calendar coverage."""


class ContractSpec(Schema):
    effective_from: date
    multiplier_ntd: int = Field(gt=0)
    tick_points: int = Field(gt=0)
    day_open: time
    day_close: time
    expiry_close: time
    night_open: time | None = None
    night_close: time | None = None

    @model_validator(mode="after")
    def validate_hours(self) -> Self:
        hours = (
            self.day_open,
            self.day_close,
            self.expiry_close,
            self.night_open,
            self.night_close,
        )
        if any(hour is not None and hour.tzinfo is not None for hour in hours):
            raise ValueError("session hours must be local wall-clock times")
        if not self.day_open < self.expiry_close <= self.day_close:
            raise ValueError("expected day_open < expiry_close <= day_close")
        if (self.night_open is None) != (self.night_close is None):
            raise ValueError("night_open and night_close must be set together")
        if (
            self.night_open is not None
            and self.night_close is not None
            and not self.night_close < self.day_open < self.day_close < self.night_open
        ):
            raise ValueError("night session must cross midnight without overlapping the day")
        return self


@dataclass(frozen=True)
class Session:
    kind: SessionKind
    trade_date: date
    opens_at: datetime
    closes_at: datetime


class TradingCalendar(Schema):
    covered_from: date
    covered_through: date
    closed_dates: frozenset[date] = frozenset()
    extra_open_dates: frozenset[date] = frozenset()
    cancelled_night_starts: frozenset[date] = frozenset()
    contracts: dict[str, tuple[ContractSpec, ...]]
    expiry_overrides: dict[str, dict[str, date]] = Field(default_factory=dict)
    cash_close: time

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.covered_from > self.covered_through:
            raise ValueError("calendar coverage dates are reversed")
        if self.cash_close.tzinfo is not None:
            raise ValueError("cash_close must be a local wall-clock time")
        if self.closed_dates & self.extra_open_dates:
            raise ValueError("a date cannot be both open and closed")
        for day in self.closed_dates | self.extra_open_dates | self.cancelled_night_starts:
            self._require_covered(day)
        for instrument, terms in self.contracts.items():
            dates = [term.effective_from for term in terms]
            if not dates or dates != sorted(set(dates)) or dates[0] > self.covered_from:
                raise ValueError(f"invalid effective dates for {instrument}")
        for instrument, overrides in self.expiry_overrides.items():
            if instrument not in self.contracts:
                raise ValueError(f"unknown expiry override instrument: {instrument}")
            for contract_month, day in overrides.items():
                self._nominal_expiry(instrument, contract_month)
                if not self.is_trade_date(day):
                    raise ValueError(f"expiry override must be a trading day: {day}")
        return self

    def _require_covered(self, day: date) -> None:
        if not self.covered_from <= day <= self.covered_through:
            raise CalendarGap(f"calendar coverage missing for {day}")

    def is_trade_date(self, day: date) -> bool:
        self._require_covered(day)
        return day in self.extra_open_dates or (day.weekday() < 5 and day not in self.closed_dates)

    def next_trade_date(self, day: date) -> date:
        self._require_covered(day)
        candidate = day + ONE_DAY
        while not self.is_trade_date(candidate):
            candidate += ONE_DAY
        return candidate

    def previous_trade_date(self, day: date) -> date:
        self._require_covered(day)
        candidate = day - ONE_DAY
        while not self.is_trade_date(candidate):
            candidate -= ONE_DAY
        return candidate

    def spec(self, instrument: str, trade_date: date) -> ContractSpec:
        self._require_covered(trade_date)
        if instrument not in self.contracts:
            raise ValueError(f"unsupported instrument: {instrument}")
        return next(
            spec
            for spec in reversed(self.contracts[instrument])
            if spec.effective_from <= trade_date
        )

    @staticmethod
    def _nominal_expiry(instrument: str, contract_month: str) -> date:
        month, separator, week = contract_month.partition("W")
        if len(month) != 6 or not month.isascii() or not month.isdigit():
            raise ValueError(f"invalid contract month: {contract_month}")
        if separator and (instrument != "MTX" or week not in {"1", "2", "4", "5"}):
            raise ValueError(f"unsupported weekly contract: {instrument} {contract_month}")
        first = date(int(month[:4]), int(month[4:]), 1)
        nth = int(week) if separator else 3
        expiry = first + timedelta(days=(2 - first.weekday()) % 7 + 7 * (nth - 1))
        if expiry.month != first.month:
            raise ValueError(f"no week {week} expiry in {month}")
        return expiry

    def last_trade_date(self, instrument: str, contract_month: str) -> date:
        override = self.expiry_overrides.get(instrument, {}).get(contract_month)
        if override is not None:
            return override
        day = self._nominal_expiry(instrument, contract_month)
        self.spec(instrument, day)
        return day if self.is_trade_date(day) else self.next_trade_date(day)

    def _expiry_as_of(self, instrument: str, contract_month: str | None, day: date) -> date | None:
        if contract_month is None:
            return None
        override = self.expiry_overrides.get(instrument, {}).get(contract_month)
        if override is not None:
            return override
        if self._nominal_expiry(instrument, contract_month) > day:
            return None
        return self.last_trade_date(instrument, contract_month)

    def day_session(
        self, instrument: str, trade_date: date, contract_month: str | None = None
    ) -> Session | None:
        spec = self.spec(instrument, trade_date)
        if not self.is_trade_date(trade_date):
            return None
        expiry = self._expiry_as_of(instrument, contract_month, trade_date)
        if expiry is not None and trade_date > expiry:
            return None
        close = spec.expiry_close if trade_date == expiry else spec.day_close
        return Session(
            "day",
            trade_date,
            datetime.combine(trade_date, spec.day_open, TAIPEI),
            datetime.combine(trade_date, close, TAIPEI),
        )

    def night_session(
        self, instrument: str, trade_date: date, contract_month: str | None = None
    ) -> Session | None:
        if not self.is_trade_date(trade_date):
            return None
        spec = self.spec(instrument, trade_date)
        if spec.night_open is None or spec.night_close is None:
            return None
        opens_on = self.previous_trade_date(trade_date)
        expiry = self._expiry_as_of(instrument, contract_month, opens_on)
        if opens_on in self.cancelled_night_starts or (expiry is not None and opens_on >= expiry):
            return None
        return Session(
            "night",
            trade_date,
            datetime.combine(opens_on, spec.night_open, TAIPEI),
            datetime.combine(opens_on + ONE_DAY, spec.night_close, TAIPEI),
        )

    def session_at(
        self, instrument: str, event_at: datetime, contract_month: str | None = None
    ) -> Session | None:
        if event_at.utcoffset() is None:
            raise ValueError("session_at requires a timezone-aware datetime")
        local_at = event_at.astimezone(TAIPEI)
        day = local_at.date()
        day_session = self.day_session(instrument, day, contract_month)
        if day_session and day_session.opens_at <= local_at <= day_session.closes_at:
            return day_session
        for opens_on in (day - ONE_DAY, day):
            if (
                opens_on < self.covered_from
                and local_at.time() >= self.spec(instrument, day).day_open
            ):
                continue
            if not self.is_trade_date(opens_on):
                continue
            try:
                trade_date = self.next_trade_date(opens_on)
            except CalendarGap:
                if opens_on == day:
                    night_open = self.spec(instrument, day).night_open
                    if night_open is None or local_at.time() < night_open:
                        continue
                raise
            night_session = self.night_session(instrument, trade_date, contract_month)
            if night_session and night_session.opens_at <= local_at <= night_session.closes_at:
                return night_session
        return None
