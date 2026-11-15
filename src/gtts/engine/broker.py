"""Validate executable bars and quote simulated fills."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from gtts.data.calendar import ContractSpec, Session, TradingCalendar
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
class OpeningBar:
    """Information independently observed by bar start."""

    key: Contract
    session: Session
    price: Decimal | None
    volume: int | None
    remaining: int
    multiplier: int
    tick: int

    def available_volume(self) -> int:
        if self.price is None:
            raise CannotFill("missing_open_price", "no opening price available by bar start")
        if self.volume is None:
            raise CannotFill("missing_open_volume", "no opening volume available by bar start")
        return self.remaining


@dataclass
class Bar:
    feature: FeatureEvent
    session: Session
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    observed_open: Decimal | None
    total_volume: int
    intrabar_volume: int
    remaining: int
    multiplier: int
    tick: int

    @property
    def key(self) -> Contract:
        return self.feature.instrument_id, self.feature.contract_month or ""


def _known_at_open(available_at: object, bar_start: datetime) -> bool:
    return (
        isinstance(available_at, datetime)
        and available_at.utcoffset() == timedelta(hours=8)
        and available_at <= bar_start
    )


def _bar_session(feature: FeatureEvent, calendar: TradingCalendar) -> tuple[Session, ContractSpec]:
    instrument, day, month = feature.instrument_id, feature.trade_date, feature.contract_month
    if instrument not in {"TX", "MTX"} or not month:
        raise CannotFill("unknown_contract", "execution requires a real TX/MTX contract month")
    if feature.bar_end - feature.bar_start != INTERVAL:
        raise CannotFill("incomplete_bar", "expected a five-minute bar")
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
    return session, spec


def _price(value: object, tick: int) -> Decimal | None:
    if not isinstance(value, (Decimal, int)) or isinstance(value, bool):
        return None
    price = Decimal(value)
    return price if price.is_finite() and price > 0 and not price % tick else None


def opening_bar(feature: FeatureEvent, calendar: TradingCalendar) -> OpeningBar:
    session, spec = _bar_session(feature, calendar)
    price = (
        _price(feature.values.get("open"), spec.tick_points)
        if _known_at_open(feature.open_price_available_at, feature.bar_start)
        else None
    )
    volume = feature.open_volume
    if (
        type(volume) is not int
        or volume < 0
        or not _known_at_open(feature.open_volume_available_at, feature.bar_start)
    ):
        volume = None
    return OpeningBar(
        (feature.instrument_id, feature.contract_month or ""),
        session,
        price,
        volume,
        volume or 0,
        spec.multiplier_ntd,
        spec.tick_points,
    )


def executable_bar(
    feature: FeatureEvent, calendar: TradingCalendar, *, opened: OpeningBar | None = None
) -> Bar:
    opened = opened if opened is not None else opening_bar(feature, calendar)
    if feature.available_at < feature.bar_end:
        raise CannotFill("incomplete_bar", "feature must be available after its five-minute close")
    prices: list[Decimal] = []
    for field in ("open", "high", "low", "close"):
        value = feature.values.get(field)
        if value is None:
            raise CannotFill("incomplete_bar", f"bar is missing {field}")
        price = _price(value, opened.tick)
        if price is None:
            raise CannotFill("invalid_price", f"{field} must be a positive tick-aligned price")
        prices.append(price)
    op, high, low, close = prices
    if not low <= min(op, close) <= max(op, close) <= high:
        raise CannotFill("invalid_price", "bar open or close is outside its high-low range")
    volume = feature.values.get("volume")
    if (
        not isinstance(volume, (Decimal, int))
        or isinstance(volume, bool)
        or not Decimal(volume).is_finite()
        or volume < 0
        or volume != int(volume)
    ):
        raise CannotFill("invalid_volume", "bar volume must be a non-negative whole count")
    if opened.volume is not None and opened.volume > volume:
        raise CannotFill("invalid_open_volume", "opening volume must be within total bar volume")
    halted = feature.values.get("halted", 0)
    if halted not in (0, 1):
        raise CannotFill("unknown_trading_status", "bar has an unknown halt flag")
    total_volume = 0 if halted else int(volume)
    intrabar_volume = 0 if halted else total_volume - (opened.volume or 0)
    return Bar(
        feature,
        opened.session,
        op,
        high,
        low,
        close,
        opened.price if opened.volume else None,
        total_volume,
        intrabar_volume,
        intrabar_volume,
        opened.multiplier,
        opened.tick,
    )


def other(side: Side) -> Side:
    return "sell" if side == "buy" else "buy"


def execution_terms(
    bar: Bar | OpeningBar, ref: Decimal, side: Side, quantity: int, schedules: Schedules
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    costs = schedules.cost_at(bar.key[0], bar.session.trade_date)
    if costs is None:
        raise CannotFill("missing_cost", "no effective fee, tax and slippage schedule")
    slip_points = Decimal(bar.tick * costs.slippage_ticks)
    price = ref + slip_points if side == "buy" else ref - slip_points
    if price <= 0:
        raise CannotFill("invalid_price", "slippage produced a non-positive execution price")
    units = bar.multiplier * quantity
    return (
        price,
        costs.broker_fee_per_contract * quantity,
        price * units * costs.tax_rate,
        slip_points * units,
    )


def quote(
    order: BacktestOrder,
    bar: Bar | OpeningBar,
    ref: Decimal,
    at: datetime,
    schedules: Schedules,
) -> BacktestFill:
    opening = isinstance(bar, OpeningBar)
    available = bar.available_volume() if isinstance(bar, OpeningBar) else bar.remaining
    if available < order.quantity:
        interval = "opening" if opening else "intrabar"
        raise CannotFill("insufficient_volume", f"{interval} volume is insufficient or halted")
    price, fee, tax, slippage = execution_terms(bar, ref, order.side, order.quantity, schedules)
    return BacktestFill(
        fill_id=order.order_id,
        order_id=order.order_id,
        signal_id=order.signal_id,
        instrument_id=order.instrument_id,
        contract_month=order.contract_month,
        trade_date=bar.session.trade_date,
        reason=order.reason,
        side=order.side,
        quantity=order.quantity,
        price=price,
        reference_price=ref,
        signal_at=order.order_at,
        fill_at=at,
        multiplier=bar.multiplier,
        gross_pnl=ZERO,
        broker_fee=fee,
        tax=tax,
        slippage=slippage,
    )
