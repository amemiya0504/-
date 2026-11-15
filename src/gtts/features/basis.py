"""Synchronized day-session basis with explicit carry and point-in-time inputs."""

from datetime import datetime, time, timedelta
from decimal import Context, Decimal, localcontext
from typing import Literal, Self

from pydantic import field_validator, model_validator

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import Schema, _taipei_datetime
from gtts.engine.broker import executable_bar

BASIS_LOOKBACK = 60
BAR_INTERVAL = timedelta(minutes=5)
CASH_OPEN = time(9, 0)
YEAR_SECONDS = Decimal(365 * 24 * 60 * 60)


class CarryInputs(Schema):
    annual_rate: Decimal | None = None
    rate_available_at: datetime | None = None
    dividend_status: Literal["none", "modeled", "unknown"]
    dividend_pv: Decimal | None = None
    dividend_available_at: datetime | None = None

    @field_validator("rate_available_at", "dividend_available_at")
    @classmethod
    def timestamps_use_taipei(cls, value: datetime | None) -> datetime | None:
        return _taipei_datetime(value) if value is not None else None

    @field_validator("annual_rate", "dividend_pv")
    @classmethod
    def carry_values_are_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("carry values must be finite")
        return value

    @model_validator(mode="after")
    def carry_fields_match(self) -> Self:
        if (self.annual_rate is None) != (self.rate_available_at is None):
            raise ValueError("annual_rate and rate_available_at must be supplied together")
        if self.dividend_status == "modeled":
            if self.dividend_pv is None or self.dividend_available_at is None:
                raise ValueError("modeled dividends require a present value and availability time")
            if self.dividend_pv < 0:
                raise ValueError("dividend_pv must be non-negative")
        elif self.dividend_pv is not None or self.dividend_available_at is not None:
            raise ValueError("dividend data is only valid when status is modeled")
        return self


class BasisPoint(Schema):
    spot: FeatureEvent
    future: FeatureEvent
    available_at: datetime
    spot_price: Decimal
    future_price: Decimal
    reference_price: Decimal
    residual: Decimal
    tau_years: Decimal
    basis_kind: Literal["fair_value", "simplified"]

    _use_taipei = field_validator("available_at")(_taipei_datetime)


def _price(event: FeatureEvent) -> Decimal | None:
    value = event.values.get("close")
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        return None
    price = Decimal(value)
    return price if price.is_finite() and price > 0 else None


def make_basis_point(
    spot: FeatureEvent,
    future: FeatureEvent,
    carry: CarryInputs,
    calendar: TradingCalendar,
) -> BasisPoint | None:
    """Return an exact-time MTX/TAIEX day pair; unknown dividend risk fails closed."""
    if (
        spot.instrument_id != "TAIEX"
        or future.instrument_id != "MTX"
        or future.contract_month is None
        or spot.session != "day"
        or future.session != "day"
        or spot.trade_date != future.trade_date
        or spot.bar_start != future.bar_start
        or spot.bar_end != future.bar_end
        or carry.dividend_status == "unknown"
    ):
        return None
    volume = future.values.get("volume")
    halted = future.values.get("halted")
    if (
        isinstance(volume, bool)
        or not isinstance(volume, (Decimal, int))
        or not Decimal(volume).is_finite()
        or volume <= 0
        or halted not in (None, 0, False)
    ):
        return None
    cash_open = datetime.combine(future.trade_date, CASH_OPEN, future.bar_start.tzinfo)
    cash_close = datetime.combine(future.trade_date, calendar.cash_close, future.bar_start.tzinfo)
    if (
        spot.bar_start.date() != future.trade_date
        or spot.bar_start < cash_open
        or spot.bar_end > cash_close
        or (spot.bar_start - cash_open) % BAR_INTERVAL
    ):
        return None
    try:
        if not calendar.is_trade_date(future.trade_date):
            return None
        session = calendar.session_at("MTX", future.bar_start, future.contract_month)
        if (
            session is None
            or session.kind != "day"
            or session.trade_date != future.trade_date
            or (future.bar_start - session.opens_at) % BAR_INTERVAL
            or future.bar_end > session.closes_at
            or calendar.is_last_trade_date("MTX", future.contract_month, future.trade_date)
        ):
            return None
        expiry = calendar.last_trade_date("MTX", future.contract_month)
        expiry_session = calendar.day_session("MTX", expiry, future.contract_month)
        executable_bar(future, calendar)
    except ValueError:
        return None
    if expiry_session is None:
        return None
    decision_at = max(spot.available_at, future.available_at)
    spot_price, future_price = _price(spot), _price(future)
    if spot_price is None or future_price is None:
        return None
    if carry.dividend_status == "modeled":
        dividend_pv = carry.dividend_pv
        dividend_available_at = carry.dividend_available_at
        if (
            dividend_pv is None
            or dividend_available_at is None
            or dividend_available_at > decision_at
        ):
            return None
    else:
        dividend_pv = Decimal(0)
    reference = spot_price - dividend_pv
    if reference <= 0:
        return None
    remaining = expiry_session.closes_at - future.bar_end
    if remaining <= timedelta(0):
        return None
    tau = Decimal(str(remaining.total_seconds())) / YEAR_SECONDS
    annual_rate = carry.annual_rate
    rate_available_at = carry.rate_available_at
    rate_is_available = (
        annual_rate is not None
        and rate_available_at is not None
        and rate_available_at <= decision_at
    )
    if rate_is_available and annual_rate is not None:
        with localcontext(Context(prec=50)):
            reference *= (annual_rate * tau).exp()
    return BasisPoint(
        spot=spot,
        future=future,
        available_at=decision_at,
        spot_price=spot_price,
        future_price=future_price,
        reference_price=reference,
        residual=future_price - reference,
        tau_years=tau,
        basis_kind="fair_value" if rate_is_available else "simplified",
    )


def past_zscore(value: Decimal, previous: list[Decimal]) -> Decimal | None:
    if len(previous) < BASIS_LOOKBACK:
        return None
    sample = previous[-BASIS_LOOKBACK:]
    mean = sum(sample, Decimal(0)) / len(sample)
    variance = sum(((item - mean) ** 2 for item in sample), Decimal(0)) / (len(sample) - 1)
    if variance <= 0:
        return None
    return (value - mean) / variance.sqrt()


def basis_zscore(current: BasisPoint, history: list[BasisPoint]) -> Decimal | None:
    previous: dict[datetime, Decimal] = {}
    for row in history:
        if (
            row.basis_kind == current.basis_kind
            and row.future.contract_month == current.future.contract_month
            and row.future.bar_start < current.future.bar_start
            and row.available_at <= current.available_at
        ):
            if row.future.bar_start in previous:
                return None
            previous[row.future.bar_start] = row.residual
    return past_zscore(current.residual, [previous[at] for at in sorted(previous)])
