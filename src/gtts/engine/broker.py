"""Validate executable bars and quote simulated fills."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from gtts.data.calendar import Session, TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.engine.events import BacktestFill, BacktestOrder, Contract, Side
from gtts.engine.portfolio import ZERO
from gtts.risk.margin import Schedules

INTERVAL = timedelta(minutes=5)
AFTER = timedelta(microseconds=1)


class CannotFill(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass
class Bar:
    feature: FeatureEvent
    session: Session
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    remaining: int
    multiplier: int
    tick: int

    @property
    def key(self) -> Contract:
        return self.feature.instrument_id, self.feature.contract_month or ""


def executable_bar(feature: FeatureEvent, calendar: TradingCalendar) -> Bar:
    instrument, day, month = feature.instrument_id, feature.trade_date, feature.contract_month
    if instrument not in {"TX", "MTX"} or not month:
        raise CannotFill("unknown_contract", "execution requires a real TX/MTX contract month")
    if feature.bar_end - feature.bar_start != INTERVAL or feature.available_at < feature.bar_end:
        raise CannotFill("incomplete_bar", "feature must cover a completed five-minute bar")
    try:
        session = (
            calendar.day_session(instrument, day, month)
            if feature.session == "day"
            else calendar.night_session(instrument, day, month)
        )
        spec = calendar.spec(instrument, day)
    except ValueError as exc:
        raise CannotFill("unknown_contract", str(exc)) from exc
    if session is None or not (
        session.opens_at <= feature.bar_start < feature.bar_end <= session.closes_at
        and (feature.bar_start - session.opens_at) % INTERVAL == timedelta(0)
    ):
        raise CannotFill(
            "closed_or_unlisted", "bar is outside its listed contract's declared session"
        )
    prices: list[Decimal] = []
    for field in ("open", "high", "low", "close"):
        value = feature.values.get(field)
        if value is None:
            raise CannotFill("incomplete_bar", f"bar is missing {field}")
        price = Decimal(value)
        if not price.is_finite() or price <= 0 or price % spec.tick_points:
            raise CannotFill("invalid_price", f"{field} must be a positive tick-aligned price")
        prices.append(price)
    op, high, low, close = prices
    if not low <= min(op, close) <= max(op, close) <= high:
        raise CannotFill("invalid_price", "bar open or close is outside its high-low range")
    volume = feature.values.get("volume")
    if volume is None or not Decimal(volume).is_finite() or volume < 0 or volume != int(volume):
        raise CannotFill("invalid_volume", "bar volume must be a non-negative whole count")
    halted = feature.values.get("halted", 0)
    if halted not in (0, 1):
        raise CannotFill("unknown_trading_status", "bar has an unknown halt flag")
    return Bar(
        feature,
        session,
        op,
        high,
        low,
        close,
        0 if halted else int(volume),
        spec.multiplier_ntd,
        spec.tick_points,
    )


def other(side: Side) -> Side:
    return "sell" if side == "buy" else "buy"


def quote(
    order: BacktestOrder, bar: Bar, ref: Decimal, at: datetime, schedules: Schedules
) -> BacktestFill:
    if bar.remaining < order.quantity:
        raise CannotFill("insufficient_volume", "bar volume is insufficient or trading is halted")
    costs = schedules.cost_at(order.instrument_id, bar.feature.trade_date)
    if costs is None:
        raise CannotFill("missing_cost", "no effective fee, tax and slippage schedule")
    slip_points = Decimal(bar.tick * costs.slippage_ticks)
    price = ref + slip_points if order.side == "buy" else ref - slip_points
    if price <= 0:
        raise CannotFill("invalid_price", "slippage produced a non-positive execution price")
    units = bar.multiplier * order.quantity
    return BacktestFill(
        fill_id=order.order_id,
        order_id=order.order_id,
        signal_id=order.signal_id,
        instrument_id=order.instrument_id,
        contract_month=order.contract_month,
        trade_date=bar.feature.trade_date,
        reason=order.reason,
        side=order.side,
        quantity=order.quantity,
        price=price,
        reference_price=ref,
        signal_at=order.order_at,
        fill_at=at,
        multiplier=bar.multiplier,
        gross_pnl=ZERO,
        broker_fee=costs.broker_fee_per_contract * order.quantity,
        tax=price * units * costs.tax_rate,
        slippage=slip_points * units,
    )
