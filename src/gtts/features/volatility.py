"""Past-only volatility measures shared by research strategies."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from gtts.data.calendar import CalendarGap, Session, TradingCalendar
from gtts.data.quality import FeatureEvent, features_at
from gtts.engine.broker import INTERVAL, Bar, CannotFill, executable_bar


def atr14(
    decision: Bar,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    *,
    decision_at: datetime | None = None,
) -> Decimal | None:
    cutoff = decision_at or decision.feature.available_at
    if decision.feature.bar_end > cutoff or decision.feature.available_at > cutoff:
        return None
    candidates = sorted(
        (
            row
            for row in history
            if row.session == "day"
            and (row.instrument_id, row.contract_month) == decision.key
            and row.bar_start < decision.feature.bar_start
            and row.available_at <= cutoff
        ),
        key=lambda row: row.bar_start,
    )
    if len(candidates) < 14 or len({row.bar_start for row in candidates}) != len(candidates):
        return None
    previous, *bars = [executable_bar(row, calendar) for row in candidates[-14:]] + [decision]
    ranges: list[Decimal] = []
    for current in bars:
        if not previous.total_volume or not current.total_volume:
            return None
        if previous.feature.bar_end != current.feature.bar_start and not (
            previous.feature.bar_end == previous.session.closes_at
            and current.feature.bar_start == current.session.opens_at
            and previous.session.trade_date
            == calendar.previous_trade_date(current.session.trade_date)
        ):
            return None
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
        previous = current
    return sum(ranges, Decimal(0)) / len(ranges)


@dataclass(frozen=True)
class BreakoutFeatures:
    atr: Decimal
    volatility_percentile: Decimal | None
    volume_zscore: Decimal | None
    donchian_high: Decimal
    donchian_low: Decimal

    @property
    def high_volatility(self) -> bool:
        return self.volatility_percentile is not None and self.volatility_percentile >= Decimal(
            "0.8"
        )


def _adjacent(previous: Bar, current: Bar) -> bool:
    return (
        previous.session.trade_date == current.session.trade_date
        and previous.feature.bar_end == current.feature.bar_start
    )


def donchian_channel(
    feature: FeatureEvent,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    *,
    count: int = 12,
    decision_at: datetime | None = None,
) -> tuple[Decimal, Decimal] | None:
    """Use only the immediately preceding complete bars from the same day session."""
    cutoff = decision_at or feature.available_at
    if (
        count < 1
        or feature.session != "day"
        or feature.available_at > cutoff
        or feature.bar_end > cutoff
    ):
        return None
    try:
        if not features_at((feature,), cutoff, calendar):
            return None
        current = executable_bar(feature, calendar)
        rows = list(history)
        raw_candidates = [
            row
            for row in rows
            if row.instrument_id == feature.instrument_id
            and row.contract_month == feature.contract_month
            and row.trade_date == feature.trade_date
            and row.session == "day"
            and row.bar_start < feature.bar_start
            and row.bar_end <= cutoff
            and row.available_at <= cutoff
        ]
        if len(raw_candidates) < count:
            return None
        candidates = [row for row in features_at(raw_candidates, cutoff, calendar)]
        if len({row.bar_start for row in candidates}) != len(candidates):
            return None
        previous = sorted(candidates, key=lambda row: row.bar_start)
        if len(previous) < count:
            return None
        bars = [executable_bar(row, calendar) for row in previous[-count:]]
        if any(not _adjacent(a, b) for a, b in pairwise(bars + [current])):
            return None
        return max(bar.high for bar in bars), min(bar.low for bar in bars)
    except (CalendarGap, CannotFill, ValueError):
        return None


def _complete_day_sessions(
    rows: list[FeatureEvent],
    feature: FeatureEvent,
    decision_at: datetime,
    calendar: TradingCalendar,
    lookback: int,
) -> list[tuple[Session, list[Bar]]] | None:
    try:
        dates: list[date] = []
        day = feature.trade_date
        for _ in range(lookback):
            day = calendar.previous_trade_date(day)
            dates.append(day)
        relevant = [
            row
            for row in rows
            if row.instrument_id == feature.instrument_id
            and row.contract_month == feature.contract_month
            and row.session == "day"
            and row.trade_date in dates
        ]
        available_dates = {
            row.trade_date
            for row in relevant
            if row.instrument_id == feature.instrument_id
            and row.bar_start < feature.bar_start
            and row.bar_end <= decision_at
            and row.available_at <= decision_at
        }
        if not set(dates).issubset(available_dates):
            return None
        available = features_at(relevant, decision_at, calendar)
        grouped: dict[date, dict[datetime, Bar]] = defaultdict(dict)
        for row in available:
            if (
                row.instrument_id != feature.instrument_id
                or row.contract_month != feature.contract_month
                or row.session != "day"
                or row.trade_date not in dates
                or row.bar_start >= feature.bar_start
            ):
                continue
            bar = executable_bar(row, calendar)
            if row.bar_start in grouped[row.trade_date]:
                return None
            grouped[row.trade_date][row.bar_start] = bar
        result: list[tuple[Session, list[Bar]]] = []
        for trade_date in reversed(dates):
            session = calendar.day_session(
                feature.instrument_id, trade_date, feature.contract_month
            )
            if session is None or (session.closes_at - session.opens_at) % INTERVAL:
                return None
            starts = []
            start = session.opens_at
            while start + INTERVAL <= session.closes_at:
                starts.append(start)
                start += INTERVAL
            bars_by_start = grouped.get(trade_date, {})
            if set(bars_by_start) != set(starts):
                return None
            result.append((session, [bars_by_start[start] for start in starts]))
        return result
    except (CalendarGap, CannotFill, ValueError):
        return None


def breakout_features(
    feature: FeatureEvent,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    *,
    decision_at: datetime | None = None,
    include_regime: bool = True,
) -> BreakoutFeatures | None:
    """Compute past-only volatility, volume and Donchian features for one complete day bar."""
    cutoff = decision_at or feature.available_at
    if feature.session != "day" or feature.available_at > cutoff or feature.bar_end > cutoff:
        return None
    rows = list(history)
    try:
        channel = donchian_channel(feature, rows, calendar, decision_at=cutoff)
        if channel is None:
            return None
        current = executable_bar(feature, calendar)
        if not current.total_volume:
            return None
        atr = atr14(current, rows, calendar, decision_at=cutoff)
        if atr is None or atr <= 0:
            return None
        percentile: Decimal | None = None
        volume_zscore: Decimal | None = None
        if include_regime:
            sessions = _complete_day_sessions(rows, feature, cutoff, calendar, 20)
            if sessions is None:
                return None
            daily_volatility = []
            for _, bars in sessions:
                traded = [bar for bar in bars if bar.total_volume]
                if not traded:
                    return None
                daily_volatility.append(
                    (max(bar.high for bar in traded) - min(bar.low for bar in traded))
                    / traded[0].open
                )
            latest = daily_volatility[-1]
            less = sum(value < latest for value in daily_volatility)
            equal = sum(value == latest for value in daily_volatility)
            percentile = Decimal(2 * less + equal + 1) / Decimal(40)

            session = calendar.day_session(
                feature.instrument_id, feature.trade_date, feature.contract_month
            )
            if session is None or (feature.bar_start - session.opens_at) % INTERVAL:
                return None
            slot = feature.bar_start.time()
            volume_sample = [
                Decimal(
                    next(bar.total_volume for bar in bars if bar.feature.bar_start.time() == slot)
                )
                for _, bars in sessions
            ]
            mean_volume = sum(volume_sample, Decimal(0)) / Decimal(len(volume_sample))
            volume_std = (
                sum(((value - mean_volume) ** 2 for value in volume_sample), Decimal(0))
                / Decimal(len(volume_sample))
            ).sqrt()
            if volume_std:
                volume_zscore = (Decimal(current.total_volume) - mean_volume) / volume_std
        return BreakoutFeatures(
            atr,
            percentile,
            volume_zscore,
            channel[0],
            channel[1],
        )
    except (CalendarGap, CannotFill, ValueError, StopIteration):
        return None
