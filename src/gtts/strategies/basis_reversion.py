"""Single-leg MTX basis reversion and same-risk price-only comparison."""

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import SignalEvent, _taipei_datetime
from gtts.engine.backtest import run_backtest
from gtts.engine.broker import AFTER, INTERVAL, CannotFill, executable_bar
from gtts.engine.events import BacktestResult, OrderRequest, Side
from gtts.features.basis import (
    BasisPoint,
    CarryInputs,
    basis_zscore,
    make_basis_point,
    past_zscore,
)
from gtts.features.volatility import atr14
from gtts.risk.limits import RiskLimits
from gtts.risk.margin import Schedules

ENTRY_Z = Decimal("2.0")
EXIT_Z = Decimal("0.5")
STOP_ATR = Decimal("1.5")


@dataclass(frozen=True)
class BasisComparison:
    basis: BacktestResult
    price_only: BacktestResult


def _key(point: BasisPoint) -> tuple[str, datetime]:
    return point.future.contract_month or "", point.future.bar_start


def _price_zscores(points: list[BasisPoint]) -> dict[tuple[str, datetime], Decimal | None]:
    result: dict[tuple[str, datetime], Decimal | None] = {}
    for current in points:
        previous = [
            row.future_price
            for row in points
            if row.future.contract_month == current.future.contract_month
            and row.future.bar_start < current.future.bar_start
            and row.available_at <= current.available_at
        ]
        result[_key(current)] = past_zscore(current.future_price, previous)
    return result


def _run_policy(
    policy: str,
    zscores: Mapping[tuple[str, datetime], Decimal | None],
    points: list[BasisPoint],
    futures: list[FeatureEvent],
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    before: datetime | None,
) -> BacktestResult:
    point_by_key = {_key(point): point for point in points}
    bars_by_date_month: dict[tuple[date, str], list[tuple[datetime, datetime]]] = {}
    for row in futures:
        if row.instrument_id == "MTX" and row.session == "day" and row.contract_month:
            try:
                bar = executable_bar(row, calendar)
            except (CalendarGap, CannotFill):
                continue
            if not bar.total_volume:
                continue
            bars_by_date_month.setdefault((row.trade_date, row.contract_month), []).append(
                (row.bar_start, row.available_at)
            )
    contract_history: dict[date, dict[str, tuple[list[datetime], list[datetime]]]] = {}
    for (day, month), rows in bars_by_date_month.items():
        starts: list[datetime] = []
        earliest_available: list[datetime] = []
        earliest: datetime | None = None
        for start, available in sorted(rows):
            earliest = available if earliest is None else min(earliest, available)
            starts.append(start)
            earliest_available.append(earliest)
        contract_history.setdefault(day, {})[month] = (starts, earliest_available)

    def front_month_at(feature: FeatureEvent) -> str | None:
        candidates: list[tuple[date, str]] = []
        for month, (starts, available) in contract_history.get(feature.trade_date, {}).items():
            index = bisect_right(starts, feature.bar_start) - 1
            if index < 0 or available[index] > feature.available_at:
                continue
            try:
                if calendar.day_session("MTX", feature.trade_date, month) is None:
                    continue
                if calendar.is_last_trade_date("MTX", month, feature.trade_date):
                    continue
                candidates.append((calendar.last_trade_date("MTX", month), month))
            except (CalendarGap, ValueError):
                continue
        return min(candidates)[1] if candidates else None

    delayed_decisions = [
        point.future.model_copy(update={"available_at": point.available_at})
        for point in points
        if point.available_at > point.future.available_at
    ]

    def decide(
        feature: FeatureEvent, positions: Mapping[tuple[str, str], int]
    ) -> tuple[OrderRequest, ...]:
        key = (feature.contract_month or "", feature.bar_start)
        z = zscores.get(key)
        point = point_by_key.get(key)
        if z is None or point is None or feature.available_at != point.available_at:
            return ()
        quantity = positions.get(("MTX", feature.contract_month or ""), 0)
        total_mtx = sum(
            abs(current) for (instrument, _), current in positions.items() if instrument == "MTX"
        )
        if quantity and abs(z) < EXIT_Z:
            side: Side = "sell" if quantity > 0 else "buy"
            signal = SignalEvent(
                signal_id=f"{policy}:{feature.contract_month}:{feature.bar_start.isoformat()}:mean-exit",
                strategy_id=policy,
                instrument_id="MTX",
                side=side,
                decision_at=feature.available_at,
                available_at=feature.available_at,
            )
            return (
                OrderRequest(
                    signal=signal,
                    contract_month=feature.contract_month or "",
                    quantity=abs(quantity),
                    reduce_only=True,
                ),
            )
        front_month = front_month_at(feature)
        if front_month is None or feature.contract_month != front_month:
            return ()
        if total_mtx or abs(z) <= ENTRY_Z:
            return ()
        try:
            bar = executable_bar(feature, calendar)
            session = calendar.day_session("MTX", feature.trade_date, feature.contract_month)
            atr = atr14(bar, futures, calendar)
        except (CannotFill, CalendarGap):
            return ()
        if session is None or atr is None or atr <= 0:
            return ()
        exit_at = session.closes_at - INTERVAL
        if feature.available_at >= exit_at:
            return ()
        side = "sell" if z > ENTRY_Z else "buy"
        signal = SignalEvent(
            signal_id=f"{policy}:{feature.contract_month}:{feature.bar_start.isoformat()}:entry",
            strategy_id=policy,
            instrument_id="MTX",
            side=side,
            decision_at=feature.available_at,
            available_at=feature.available_at,
        )
        return (
            OrderRequest(
                signal=signal,
                contract_month=feature.contract_month or "",
                quantity=1,
                max_instrument_exposure=1,
                stop_distance=atr * STOP_ATR,
                exit_at=exit_at,
            ),
        )

    return run_backtest(
        calendar,
        schedules,
        initial_cash,
        futures,
        before=before,
        on_feature_with_positions=decide,
        decision_features=delayed_decisions,
        risk_limits=RiskLimits(),
    )


def compare_basis(
    spot_bars: Iterable[FeatureEvent],
    futures_bars: Iterable[FeatureEvent],
    carry_by_bar: Mapping[tuple[str, datetime], CarryInputs],
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    *,
    before: datetime | None = None,
) -> BasisComparison:
    """Compare spot/futures residual against futures-price mean reversion."""
    futures = list(futures_bars)
    spots = list(spot_bars)
    if before is not None:
        before = _taipei_datetime(before)
        futures = [row for row in futures if row.bar_start + AFTER < before]
        spots = [row for row in spots if row.available_at < before]
    spot_by_start = {row.bar_start: row for row in spots}
    if len(spot_by_start) != len(spots):
        raise ValueError("duplicate TAIEX spot bar start")
    future_keys = [(row.contract_month, row.bar_start) for row in futures]
    if len(set(future_keys)) != len(future_keys):
        raise ValueError("duplicate MTX futures bar start")
    points: list[BasisPoint] = []
    for future in sorted(futures, key=lambda row: row.bar_start):
        spot = spot_by_start.get(future.bar_start)
        contract = future.contract_month or ""
        carry = carry_by_bar.get((contract, future.bar_start))
        if spot is None or carry is None:
            continue
        point = make_basis_point(spot, future, carry, calendar)
        if point is not None:
            points.append(point)
    basis_z = {
        _key(point): basis_zscore(point, points[:index]) for index, point in enumerate(points)
    }
    price_z = _price_zscores(points)
    return BasisComparison(
        basis=_run_policy(
            "basis_reversion", basis_z, points, futures, calendar, schedules, initial_cash, before
        ),
        price_only=_run_policy(
            "price_mean_reversion",
            price_z,
            points,
            futures,
            calendar,
            schedules,
            initial_cash,
            before,
        ),
    )
