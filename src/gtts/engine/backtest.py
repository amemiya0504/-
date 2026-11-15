"""Deterministic event loop for offline TX/MTX execution."""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, localcontext
from heapq import heappop, heappush
from itertools import chain

from gtts.data.calendar import CalendarGap, Session, TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import _taipei_datetime
from gtts.engine.broker import (
    AFTER,
    INTERVAL,
    Bar,
    CannotFill,
    OpeningBar,
    executable_bar,
    opening_bar,
    other,
    quote,
)
from gtts.engine.events import (
    Audit,
    BacktestFill,
    BacktestOrder,
    BacktestResult,
    BacktestSnapshot,
    Contract,
    OrderRequest,
    Reason,
    RollRequest,
    SettlementEvent,
    SettlementRecord,
)
from gtts.engine.portfolio import ZERO, Portfolio, signed
from gtts.engine.roll import roll_orders
from gtts.risk.margin import Schedules

Request = OrderRequest | RollRequest
Features = Callable[[FeatureEvent], Iterable[Request]]
PositionFeatures = Callable[[FeatureEvent, Mapping[Contract, int]], Iterable[Request]]
ONE_DAY = timedelta(days=1)


def _next_open(calendar: TradingCalendar, key: Contract, at: datetime) -> datetime:
    instrument, month = key
    if instrument not in {"TX", "MTX"}:
        raise CannotFill("unknown_contract", "only real TX/MTX contracts are executable")
    day = at.date()
    if day < calendar.covered_from:
        raise CannotFill("calendar_gap", "decision predates verified calendar coverage")
    try:
        expiry = calendar.last_trade_date(instrument, month)
    except CalendarGap:
        expiry = None

    def opening(session: Session | None) -> datetime | None:
        if session is None or session.closes_at <= at:
            return None
        start = session.opens_at
        if at > start:
            whole, fraction = divmod(at - start, INTERVAL)
            start += (whole + bool(fraction)) * INTERVAL
        return start if start + INTERVAL <= session.closes_at else None

    while day <= calendar.covered_through:
        if expiry is not None and day > expiry:
            raise CannotFill("closed_or_unlisted", "contract has expired")
        daytime = calendar.day_session(instrument, day)
        try:
            overnight = calendar.night_session(instrument, day)
        except CalendarGap:
            overnight = None
        for session in (overnight, daytime):
            if session is None or opening(session) is None:
                continue
            listed = (
                calendar.day_session(instrument, day, month)
                if session.kind == "day"
                else calendar.night_session(instrument, day, month)
            )
            next_open = opening(listed)
            if next_open is not None:
                return next_open
        day += ONE_DAY
    raise CannotFill("calendar_gap", "next opening is outside verified calendar coverage")


def _settlement_cutoff(
    calendar: TradingCalendar, key: Contract, session: Session, final: bool
) -> datetime | None:
    if final:
        return session.closes_at
    try:
        return _next_open(calendar, key, session.closes_at)
    except CannotFill as exc:
        if exc.code != "calendar_gap":
            raise
        return None


@dataclass
class _Bracket:
    entry: BacktestOrder
    entry_fill_at: datetime
    stop: Decimal | None
    target: Decimal | None
    triggered_stop: bool = False


@dataclass
class _Bucket:
    opens: dict[Contract, OpeningBar] = field(default_factory=dict)
    features: list[tuple[FeatureEvent, OpeningBar]] = field(default_factory=list)
    decisions: list[FeatureEvent] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    eligible: list[tuple[Request, tuple[BacktestOrder, ...]]] = field(default_factory=list)
    settlements: list[SettlementEvent] = field(default_factory=list)
    daily: list[tuple[Contract, date, bool]] = field(default_factory=list)
    timed_exits: set[tuple[Contract, str]] = field(default_factory=set)
    expires: set[Contract] = field(default_factory=set)
    margin_change: bool = False


class _Engine:
    def __init__(
        self,
        calendar: TradingCalendar,
        schedules: Schedules,
        cash: Decimal,
        before: datetime | None,
    ):
        self.calendar = calendar
        self.schedules = schedules
        self.before = before
        self.portfolio = Portfolio(cash)
        self.clock: list[datetime] = []
        self.buckets: dict[datetime, _Bucket] = {}
        self.orders: list[BacktestOrder] = []
        self.fills: list[BacktestFill] = []
        self.audit: list[Audit] = []
        self.mark_times: dict[Contract, tuple[datetime, bool]] = {}
        self.snapshots: dict[datetime, BacktestSnapshot] = {}
        self.settlements: list[SettlementRecord] = []
        self.signals: set[str] = set()
        self.brackets: dict[Contract, _Bracket] = {}
        self.settled: set[tuple[Contract, date]] = set()
        self.unsettled: dict[Contract, set[date]] = {}
        self.overdue_time_exits: set[Contract] = set()
        self.margin_call = False
        self.contracts: set[Contract] = set()
        self.valid_features: set[tuple[str, str, datetime]] = set()
        self.scheduled_through: datetime | None = None

    def includes(self, at: datetime) -> bool:
        return self.before is None or at < self.before

    def bucket(self, at: datetime) -> _Bucket:
        if at not in self.buckets:
            self.buckets[at] = _Bucket()
            heappush(self.clock, at)
        return self.buckets[at]

    def log(
        self, at: datetime, key: Contract, code: str, detail: str, order_id: str | None = None
    ) -> None:
        self.audit.append(
            Audit(
                at=at,
                instrument_id=key[0],
                contract_month=key[1] or None,
                code=code,
                detail=detail,
                order_id=order_id,
            )
        )

    def record_failure(self, at: datetime, order: BacktestOrder, error: CannotFill) -> None:
        self.log(at, order.contract, error.code, str(error), order.order_id)

    def mark(
        self, key: Contract, price: Decimal, multiplier: int, at: datetime, *, opening: bool
    ) -> None:
        timestamp = (at, opening)
        if key not in self.mark_times or self.mark_times[key] <= timestamp:
            self.portfolio.mark(key, price, multiplier)
            self.mark_times[key] = timestamp

    def snapshot(self, at: datetime) -> None:
        row = self.portfolio.snapshot(self.schedules, at)
        self.snapshots[at] = row
        if row.maintenance_margin is None:
            if not self.margin_call:
                for key in self.portfolio.positions:
                    if self.schedules.margin_at(key[0], at) is None:
                        self.log(at, key, "missing_margin", "held contract has no margin schedule")
            self.margin_call = True
        elif row.equity < row.maintenance_margin and self.portfolio.positions:
            if not self.margin_call:
                for key in self.portfolio.positions:
                    self.log(at, key, "margin_call", "equity is below maintenance margin")
            self.margin_call = True
        elif not self.portfolio.positions:
            self.margin_call = False

    def submit(self, request: Request, at: datetime, bucket: _Bucket) -> None:
        signal = request.signal
        month = (
            request.contract_month
            if isinstance(request, OrderRequest)
            else request.from_contract_month
        )
        key = (signal.instrument_id, month)
        if signal.signal_id in self.signals or signal.signal_id.startswith("@"):
            self.log(
                at, key, "invalid_signal_id", "signal ID is duplicate or uses reserved @ prefix"
            )
            return
        self.signals.add(signal.signal_id)
        if not signal.available_at <= signal.decision_at == at:
            self.log(at, key, "invalid_signal_time", "signal data was not available at decision")
            return
        legs: tuple[BacktestOrder, ...]
        if isinstance(request, RollRequest):
            legs = roll_orders(request)
        else:
            legs = (
                BacktestOrder(
                    order_id=f"{signal.signal_id}:order",
                    signal_id=signal.signal_id,
                    instrument_id=signal.instrument_id,
                    contract_month=request.contract_month,
                    side=signal.side,
                    quantity=request.quantity,
                    order_at=at,
                    reason="signal",
                ),
            )
        self.orders.extend(legs)
        try:
            openings = {_next_open(self.calendar, leg.contract, at) for leg in legs}
            if len(openings) != 1:
                raise CannotFill("unaligned_roll", "roll legs do not share a next opening")
        except ValueError as exc:
            error = exc if isinstance(exc, CannotFill) else CannotFill("unknown_contract", str(exc))
            for leg in legs:
                self.record_failure(at, leg, error)
            return
        start = openings.pop()
        if not self.includes(start + AFTER):
            for leg in legs:
                self.log(
                    at, leg.contract, "outside_replay", "next opening is after cutoff", leg.order_id
                )
            return
        self.schedule_through(start)
        (bucket if start == at else self.bucket(start)).eligible.append((request, legs))

    def execute(
        self,
        legs: tuple[tuple[BacktestOrder, Bar | OpeningBar, Decimal], ...],
        at: datetime,
        *,
        reduce_only: bool = False,
        max_instrument_exposure: int | None = None,
    ) -> tuple[BacktestFill, ...]:
        trial = deepcopy(self.portfolio) if len(legs) > 1 or not reduce_only else self.portfolio
        rows = []
        for order, bar, ref in legs:
            old = trial.quantity(order.contract)
            delta = signed(order.side, order.quantity)
            new = old + delta
            opens_exposure = new != 0 and (old * new <= 0 or abs(new) > abs(old))
            if opens_exposure and reduce_only:
                raise CannotFill("reduce_only", "exit would increase or reverse exposure")
            if opens_exposure and any(
                instrument == order.instrument_id for instrument, _ in self.overdue_time_exits
            ):
                raise CannotFill(
                    "overdue_time_exit", "new exposure blocked until the overdue exit is flat"
                )
            if opens_exposure and (self.margin_call or self.unsettled):
                code = "margin_call" if self.margin_call else "unsettled_account"
                raise CannotFill(code, "new exposure blocked while account risk is unresolved")
            if opens_exposure and self.schedules.margin_at(order.instrument_id, at) is None:
                raise CannotFill("missing_margin", "no effective margin schedule for new exposure")
            row = quote(order, bar, ref, at, self.schedules)
            gross = trial.trade(order.contract, delta, ref, bar.multiplier)
            row = row.model_copy(update={"gross_pnl": gross})
            trial.apply((row,))
            if (
                opens_exposure
                and max_instrument_exposure is not None
                and sum(
                    abs(quantity)
                    for (instrument, _), quantity in trial.positions.items()
                    if instrument == order.instrument_id
                )
                > max_instrument_exposure
            ):
                raise CannotFill(
                    "position_limit", "instrument exposure exceeds the order's position limit"
                )
            rows.append(row)
        if not reduce_only:
            old_positions, new_positions = self.portfolio.positions, trial.positions
            increased = any(
                quantity * old_positions.get(key, 0) <= 0
                or abs(quantity) > abs(old_positions.get(key, 0))
                for key, quantity in new_positions.items()
            )
            if increased:
                initial, maintenance = trial.margin(self.schedules, at)
                if maintenance is None or trial.cash + trial.unrealized < initial:
                    raise CannotFill(
                        "initial_margin", "equity after trade costs is below initial margin"
                    )
        self.portfolio = trial
        for (order, bar, _), row in zip(legs, rows, strict=True):
            bar.remaining -= row.quantity
            self.fills.append(row)
            key = order.contract
            quantity = self.portfolio.quantity(key)
            if not quantity:
                self.unsettled.pop(key, None)
                self.overdue_time_exits.discard(key)
            if key in self.brackets and quantity * signed(self.brackets[key].entry.side, 1) <= 0:
                del self.brackets[key]
        return tuple(rows)

    def liquidate(
        self, key: Contract, bar: OpeningBar | None, at: datetime, reason: Reason, when: datetime
    ) -> None:
        quantity = self.portfolio.quantity(key)
        if not quantity:
            return
        if bar is None:
            self.log(at, key, f"{reason}_unfilled", "forced exit has no executable price")
            return
        try:
            available = bar.available_volume()
        except CannotFill as exc:
            self.log(at, key, exc.code, f"{reason} exit: {exc}")
            return
        if not available:
            self.log(at, key, f"{reason}_unfilled", "forced exit has no remaining opening volume")
            return
        count = min(abs(quantity), available)
        signal_id = f"@{reason}:{key[0]}:{key[1]}:{when.isoformat()}"
        order = BacktestOrder(
            order_id=signal_id,
            signal_id=signal_id,
            instrument_id=key[0],
            contract_month=key[1],
            side="sell" if quantity > 0 else "buy",
            quantity=count,
            order_at=when,
            reason=reason,
        )
        self.orders.append(order)
        try:
            assert bar.price is not None
            self.execute(((order, bar, bar.price),), at, reduce_only=True)
            if self.portfolio.quantity(key):
                self.log(
                    at,
                    key,
                    f"{reason}_partial",
                    "insufficient volume to close remaining exposure",
                    order.order_id,
                )
        except CannotFill as exc:
            self.record_failure(at, order, exc)

    def protect(self, bar: Bar | OpeningBar, at: datetime) -> None:
        key = bar.key
        bracket = self.brackets.get(key)
        if (
            bracket is None
            or bracket.entry_fill_at >= at
            or (bracket.stop is None and bracket.target is None)
        ):
            return
        long = bracket.entry.side == "buy"
        opening = isinstance(bar, OpeningBar)
        if isinstance(bar, OpeningBar):
            if bar.price is None:
                if bracket.triggered_stop:
                    self.log(at, key, "missing_open_price", "pending stop has no opening price")
                return
            check_stop = check_target = bar.price
        else:
            if not bar.intrabar_volume or bracket.triggered_stop:
                return
            if bar.observed_open is None:
                self.log(
                    at + AFTER,
                    key,
                    "unknown_intrabar_extrema",
                    "opening trade evidence is required to isolate intrabar price extremes",
                    bracket.entry.order_id,
                )
                return
            check_stop = bar.low if long else bar.high
            check_target = bar.high if long else bar.low
        stop, target = bracket.stop, bracket.target
        hit_stop = bracket.triggered_stop or (
            stop is not None and (check_stop <= stop if long else check_stop >= stop)
        )
        hit_target = target is not None and (
            check_target >= target if long else check_target <= target
        )
        if isinstance(bar, Bar):
            opened = bar.observed_open
            assert opened is not None
            # An extreme already reached at the open does not prove a later trade at that level.
            hit_stop = hit_stop and stop is not None and (opened > stop if long else opened < stop)
            hit_target = (
                hit_target and target is not None and (opened < target if long else opened > target)
            )
        if not hit_stop and not hit_target:
            return
        reason: Reason = "stop" if hit_stop else "target"
        if isinstance(bar, OpeningBar):
            ref = bar.price
        elif hit_stop:
            ref = stop
        else:
            ref = target
        if ref is None:
            return
        if hit_stop and (isinstance(bar, Bar) or bar.volume):
            bracket.triggered_stop = True
        quantity = abs(self.portfolio.quantity(key))
        try:
            available = bar.available_volume() if isinstance(bar, OpeningBar) else bar.remaining
        except CannotFill as exc:
            self.log(at, key, exc.code, f"{reason} exit: {exc}", bracket.entry.order_id)
            return
        if not available:
            interval = "opening" if opening else "intrabar"
            self.log(at, key, f"{reason}_unfilled", f"no remaining {interval} volume")
            return
        count = min(quantity, available) if hit_stop else quantity
        signal_id = f"@{reason}:{bracket.entry.order_id}:{at.isoformat()}"
        order = BacktestOrder(
            order_id=signal_id,
            signal_id=signal_id,
            instrument_id=key[0],
            contract_month=key[1],
            side=other(bracket.entry.side),
            quantity=count,
            order_at=bracket.entry_fill_at,
            reason=reason,
        )
        self.orders.append(order)
        try:
            self.execute(((order, bar, ref),), at, reduce_only=True)
            if count < quantity:
                self.log(
                    at,
                    key,
                    "stop_partial",
                    "triggered stop retains remaining exposure",
                    order.order_id,
                )
        except CannotFill as exc:
            self.record_failure(at, order, exc)

    def regular(
        self, request: Request, legs: tuple[BacktestOrder, ...], at: datetime, bucket: _Bucket
    ) -> None:
        stop: Decimal | None = None
        target: Decimal | None = None
        try:
            if isinstance(request, RollRequest):
                old, new = legs
                if (
                    signed(request.signal.side, self.portfolio.quantity(old.contract))
                    < request.quantity
                    or signed(request.signal.side, self.portfolio.quantity(new.contract)) < 0
                ):
                    raise CannotFill(
                        "invalid_roll", "roll must reduce old and add same-side new exposure"
                    )
            quoted = []
            for order in legs:
                key = order.contract
                bar = bucket.opens.get(key)
                if bar is None:
                    raise CannotFill(
                        "missing_open", f"no valid bar at the next scheduled open for {key}"
                    )
                price = bar.price
                if price is None:
                    raise CannotFill(
                        "missing_open_price", "no opening price available by bar start"
                    )
                old_qty = self.portfolio.quantity(key)
                new_qty = old_qty + signed(order.side, order.quantity)
                opens = new_qty != 0 and (old_qty * new_qty <= 0 or abs(new_qty) > abs(old_qty))
                if opens and key in bucket.expires:
                    raise CannotFill("expiry", "cannot open exposure in the final trading bar")
                if opens and key in self.brackets:
                    raise CannotFill(
                        "active_bracket", "cannot add exposure while protective orders are active"
                    )
                if isinstance(request, OrderRequest):
                    stop, target = request.stop_price, request.target_price
                    levels = [p for p in (stop, target) if p is not None]
                    distance = request.stop_distance
                    target_distance = request.target_distance
                    if (
                        levels
                        or distance is not None
                        or target_distance is not None
                        or request.exit_at is not None
                    ) and (old_qty or any(p % bar.tick for p in levels)):
                        raise CannotFill(
                            "invalid_bracket",
                            "protection requires a flat start and tick-aligned prices",
                        )
                    if request.exit_at is not None and not (
                        at < request.exit_at < bar.session.closes_at
                        and (request.exit_at - bar.session.opens_at) % INTERVAL == timedelta(0)
                    ):
                        raise CannotFill(
                            "invalid_time_exit",
                            "timed exit must be a later opening in the entry session",
                        )
                    if levels or distance is not None or target_distance is not None:
                        row = quote(order, bar, price, at, self.schedules)
                        if distance is not None:
                            long = order.side == "buy"
                            level = row.price - distance if long else row.price + distance
                            rounding = ROUND_CEILING if long else ROUND_FLOOR
                            stop = (level / bar.tick).to_integral_value(
                                rounding=rounding
                            ) * bar.tick
                            if stop <= 0:
                                raise CannotFill("invalid_bracket", "stop must be positive")
                        if target_distance is not None:
                            long = order.side == "buy"
                            level = (
                                row.price + target_distance if long else row.price - target_distance
                            )
                            rounding = ROUND_FLOOR if long else ROUND_CEILING
                            target = (level / bar.tick).to_integral_value(
                                rounding=rounding
                            ) * bar.tick
                            if target <= 0:
                                raise CannotFill("invalid_bracket", "target must be positive")
                        lower, upper = (stop, target) if order.side == "buy" else (target, stop)
                        if (lower is not None and min(price, row.price) <= lower) or (
                            upper is not None and max(price, row.price) >= upper
                        ):
                            raise CannotFill(
                                "invalid_bracket",
                                "entry open or fill is outside its protective levels",
                            )
                quoted.append((order, bar, price))
            reduce_only = isinstance(request, OrderRequest) and request.reduce_only
            max_exposure = (
                request.max_instrument_exposure if isinstance(request, OrderRequest) else None
            )
            fills = self.execute(
                tuple(quoted), at, reduce_only=reduce_only, max_instrument_exposure=max_exposure
            )
            if (
                isinstance(request, OrderRequest)
                and not request.reduce_only
                and (
                    stop is not None
                    or request.target_price is not None
                    or request.target_distance is not None
                    or request.exit_at is not None
                )
            ):
                entry = legs[0]
                self.brackets[entry.contract] = _Bracket(entry, fills[0].fill_at, stop, target)
                if request.exit_at is not None and self.includes(request.exit_at + AFTER):
                    self.schedule_through(request.exit_at)
                    self.bucket(request.exit_at).timed_exits.add((entry.contract, entry.order_id))
        except CannotFill as exc:
            for order in legs:
                self.record_failure(at, order, exc)

    def settle(self, event: SettlementEvent) -> None:
        key = (event.instrument_id, event.contract_month)
        ref = key, event.trade_date
        at = event.available_at
        quantity = self.portfolio.quantity(key)
        try:
            if key[0] not in {"TX", "MTX"}:
                raise CannotFill("invalid_settlement", "settlement requires a real TX/MTX contract")
            session = self.calendar.day_session(key[0], event.trade_date, key[1])
            if session is None or at < session.closes_at:
                raise CannotFill(
                    "invalid_settlement", "settlement is unavailable before valid day close"
                )
            final = self.calendar.is_last_trade_date(*key, event.trade_date)
            if event.final != final:
                raise CannotFill("invalid_settlement", "final flag disagrees with contract expiry")
            if not final:
                cutoff = _settlement_cutoff(self.calendar, key, session, final)
                if cutoff is not None and at > cutoff:
                    raise CannotFill(
                        "late_settlement", "daily settlement arrived after next session open"
                    )
            if ref in self.settled:
                raise CannotFill("duplicate_settlement", "trade date has already been settled")
            costs = self.schedules.cost_at(key[0], event.trade_date)
            broker_fee = tax = ZERO
            multiplier = self.calendar.spec(key[0], event.trade_date).multiplier_ntd
            if quantity and final:
                if (
                    costs is None
                    or costs.final_settlement_fee_per_contract is None
                    or (costs.final_settlement_tax_rate is None)
                ):
                    raise CannotFill(
                        "missing_final_cost", "no explicit final settlement fee and tax"
                    )
                broker_fee = costs.final_settlement_fee_per_contract * abs(quantity)
                tax = event.price * multiplier * abs(quantity) * costs.final_settlement_tax_rate
            self.settled.add(ref)
            unresolved_dates = self.unsettled.get(key)
            if unresolved_dates is not None:
                unresolved_dates.discard(event.trade_date)
                if final or not unresolved_dates:
                    del self.unsettled[key]
            if quantity:
                pnl = self.portfolio.settle(event, multiplier, broker_fee, tax)
                self.mark_times[key] = (at, False)
                self.settlements.append(
                    SettlementRecord(
                        event=event,
                        quantity=quantity,
                        gross_pnl=pnl,
                        broker_fee=broker_fee,
                        tax=tax,
                    )
                )
                if final:
                    self.brackets.pop(key, None)
                    self.overdue_time_exits.discard(key)
        except ValueError as exc:
            code = exc.code if isinstance(exc, CannotFill) else "invalid_settlement"
            self.log(at, key, code, str(exc))

    def prepare(
        self,
        features: Iterable[FeatureEvent],
        opening_only: Iterable[FeatureEvent],
        decision_features: Iterable[FeatureEvent],
        orders: Iterable[Request],
        settlements: Iterable[SettlementEvent],
    ) -> None:
        execution_bars: dict[tuple[datetime, Contract], tuple[FeatureEvent, bool]] = {}
        for feature, complete in chain(
            ((row, True) for row in features),
            ((row, False) for row in opening_only),
        ):
            if not self.includes(feature.bar_start + AFTER):
                continue
            key = feature.instrument_id, feature.contract_month or ""
            bar_key = feature.bar_start, key
            if bar_key in execution_bars:
                raise ValueError(
                    f"duplicate execution bar for {key[0]}:{key[1]} at {feature.bar_start.isoformat()}"
                )
            execution_bars[bar_key] = (feature, complete)
        for (start, key), (feature, complete) in sorted(execution_bars.items()):
            try:
                bar = opening_bar(feature, self.calendar)
            except CannotFill as exc:
                self.log(start, key, exc.code, str(exc))
                continue
            self.bucket(start).opens[key] = bar
            if complete:
                available = max(feature.bar_end, feature.available_at)
                if self.includes(available):
                    self.bucket(available).features.append((feature, bar))
            self.contracts.add(key)
        source_by_key = {
            (row.instrument_id, row.contract_month or "", row.bar_start): row
            for row, complete in execution_bars.values()
            if complete
        }
        for decision in decision_features:
            if not self.includes(decision.available_at):
                continue
            decision_key = (
                decision.instrument_id,
                decision.contract_month or "",
                decision.bar_start,
            )
            source = source_by_key.get(decision_key)
            if source is None:
                raise ValueError("decision feature must match a completed execution bar")
            if decision.model_copy(update={"available_at": source.available_at}) != source:
                raise ValueError("decision feature may only change its availability time")
            if decision.available_at <= max(source.available_at, source.bar_end):
                raise ValueError("decision feature must be later than its execution bar")
            self.bucket(decision.available_at).decisions.append(decision)
        for order in orders:
            signal = order.signal
            if self.includes(signal.decision_at):
                self.bucket(signal.decision_at).requests.append(order)
        for event in settlements:
            if self.includes(event.available_at):
                self.bucket(event.available_at).settlements.append(event)
        if self.clock:
            self.schedule_through(max(self.clock) if self.before is None else self.before - AFTER)

    def schedule_through(self, last: datetime) -> None:
        if self.scheduled_through is not None:
            if last <= self.scheduled_through:
                return
            first = self.scheduled_through + AFTER
        else:
            first = min(self.clock)
        self.scheduled_through = last
        for margin_rows in self.schedules.margins.values():
            for row in margin_rows:
                if first <= row.effective_from <= last:
                    self.bucket(row.effective_from).margin_change = True
        start = max(self.calendar.covered_from, first.date())
        if start > self.calendar.covered_through:
            return
        # A prior day may have its settlement cutoff in the new window.
        try:
            start = self.calendar.previous_trade_date(start)
        except CalendarGap:
            pass
        for key in sorted(self.contracts):
            day = start
            while day <= min(last.date(), self.calendar.covered_through):
                try:
                    session = self.calendar.day_session(key[0], day, key[1])
                except ValueError:
                    break
                if session is not None:
                    final = self.calendar.is_last_trade_date(*key, day)
                    cutoff = _settlement_cutoff(self.calendar, key, session, final)
                    if cutoff is not None and first <= cutoff <= last:
                        self.bucket(cutoff).daily.append((key, day, final))
                    final_open = session.closes_at - INTERVAL
                    if final and first <= final_open <= last:
                        self.bucket(final_open).expires.add(key)
                day += ONE_DAY

    def run(
        self,
        on_feature: Features | None,
        on_feature_with_positions: PositionFeatures | None,
    ) -> BacktestResult:
        while self.clock:
            at = heappop(self.clock)
            bucket = self.buckets.pop(at)
            features = []
            for feature, opened in sorted(
                bucket.features, key=lambda item: (item[0].bar_end, item[1].key)
            ):
                try:
                    bar = executable_bar(feature, self.calendar, opened=opened)
                except CannotFill as exc:
                    self.log(at, opened.key, exc.code, str(exc))
                    continue
                features.append(feature)
                self.valid_features.add(
                    (feature.instrument_id, feature.contract_month or "", feature.bar_start)
                )
                if feature.bar_end == at:
                    self.protect(bar, at - AFTER)
                elif bar.intrabar_volume:
                    self.log(
                        at, bar.key, "late_intrabar_bar", "late OHLC cannot replay intrabar fills"
                    )
                if bar.total_volume:
                    self.mark(bar.key, bar.close, bar.multiplier, feature.bar_end, opening=False)
            settlement_groups: dict[tuple[Contract, date], list[SettlementEvent]] = defaultdict(
                list
            )
            for event in bucket.settlements:
                settlement_groups[
                    (event.instrument_id, event.contract_month), event.trade_date
                ].append(event)
            for (key, _), same in sorted(settlement_groups.items()):
                if len(same) == 1:
                    self.settle(same[0])
                else:
                    self.log(
                        at,
                        key,
                        "duplicate_settlement",
                        "ambiguous simultaneous settlement prices",
                    )
            for key, day, final in bucket.daily:
                if self.portfolio.quantity(key) and (key, day) not in self.settled:
                    self.unsettled.setdefault(key, set()).add(day)
                    if not final:
                        self.log(
                            at, key, "missing_settlement", f"no valid daily settlement for {day}"
                        )
            if features or bucket.settlements or bucket.daily or bucket.margin_change:
                self.snapshot(at)

            requests = bucket.requests
            for feature in features:
                if on_feature is not None:
                    requests.extend(on_feature(feature.model_copy(deep=True)))
                if on_feature_with_positions is not None:
                    requests.extend(
                        on_feature_with_positions(
                            feature.model_copy(deep=True), dict(self.portfolio.positions)
                        )
                    )
            if on_feature_with_positions is not None:
                for decision in bucket.decisions:
                    decision_key = (
                        decision.instrument_id,
                        decision.contract_month or "",
                        decision.bar_start,
                    )
                    if decision_key not in self.valid_features:
                        continue
                    requests.extend(
                        on_feature_with_positions(
                            decision.model_copy(deep=True), dict(self.portfolio.positions)
                        )
                    )
            counts = Counter(item.signal.signal_id for item in requests)
            for row in sorted(requests, key=lambda item: item.signal.signal_id):
                if counts[row.signal.signal_id] > 1:
                    self.log(
                        at,
                        (row.signal.instrument_id, ""),
                        "invalid_signal_id",
                        f"duplicate signal ID at decision: {row.signal.signal_id}",
                    )
                else:
                    self.submit(row, at, bucket)

            if not (bucket.opens or bucket.eligible or bucket.expires or bucket.timed_exits):
                continue
            fill_at = at + AFTER
            if not self.includes(fill_at):
                continue
            had_margin_call = self.margin_call
            for _, opened in sorted(bucket.opens.items()):
                if opened.price is not None:
                    self.mark(opened.key, opened.price, opened.multiplier, at, opening=True)
            self.snapshot(fill_at)
            for key in sorted(bucket.expires):
                self.liquidate(key, bucket.opens.get(key), fill_at, "expiry", at)
            if had_margin_call:
                for key in self.portfolio.positions:
                    if key not in bucket.expires:
                        self.liquidate(key, bucket.opens.get(key), fill_at, "margin", at)
            for _, opened in sorted(bucket.opens.items()):
                self.protect(opened, fill_at)
            for key, entry_id in sorted(bucket.timed_exits):
                bracket = self.brackets.get(key)
                if bracket is not None and bracket.entry.order_id == entry_id:
                    self.liquidate(key, bucket.opens.get(key), fill_at, "time_exit", at)
                    if self.portfolio.quantity(key):
                        self.overdue_time_exits.add(key)
                        self.log(
                            fill_at,
                            key,
                            "time_exit_unresolved",
                            "timed exit left exposure open",
                            entry_id,
                        )
            for request, legs in sorted(bucket.eligible, key=lambda row: row[1][0].order_id):
                if had_margin_call:
                    self.margin_call = True
                self.regular(request, legs, fill_at, bucket)
                self.snapshot(fill_at)
            self.snapshot(fill_at)

        if self.scheduled_through is not None:
            for key, days in sorted(self.unsettled.items()):
                for day in sorted(days):
                    if self.calendar.is_last_trade_date(*key, day):
                        self.log(
                            self.scheduled_through,
                            key,
                            "expiry_unsettled",
                            f"backtest ended without a valid final settlement for {day}",
                        )
        if self.snapshots and self.margin_call:
            for key in self.portfolio.positions:
                self.log(
                    max(self.snapshots),
                    key,
                    "margin_unresolved",
                    "backtest ended before forced liquidation completed",
                )
        return BacktestResult(
            orders=tuple(self.orders),
            fills=tuple(self.fills),
            snapshots=tuple(self.snapshots[when] for when in sorted(self.snapshots)),
            settlements=tuple(self.settlements),
            audit=tuple(
                sorted(
                    self.audit,
                    key=lambda row: (
                        row.at,
                        row.instrument_id,
                        row.contract_month or "",
                        row.order_id or "",
                        row.code,
                        row.detail,
                    ),
                )
            ),
        )


def run_backtest(
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    bars: Iterable[FeatureEvent],
    *,
    before: datetime | None = None,
    opening_only_bars: Iterable[FeatureEvent] = (),
    decision_features: Iterable[FeatureEvent] = (),
    orders: Iterable[Request] = (),
    settlements: Iterable[SettlementEvent] = (),
    on_feature: Features | None = None,
    on_feature_with_positions: PositionFeatures | None = None,
) -> BacktestResult:
    """Replay market bars and optional callback-only decisions reproducibly before a cutoff."""
    if not isinstance(initial_cash, Decimal) or not initial_cash.is_finite() or initial_cash < 0:
        raise ValueError("initial_cash must be a finite, non-negative Decimal")
    with localcontext(Context(prec=50)):
        engine = _Engine(
            calendar,
            schedules,
            initial_cash,
            _taipei_datetime(before) if before is not None else None,
        )
        engine.prepare(bars, opening_only_bars, decision_features, orders, settlements)
        return engine.run(on_feature, on_feature_with_positions)
