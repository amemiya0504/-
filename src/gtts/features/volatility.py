"""Past-only volatility measures shared by research strategies."""

from collections.abc import Iterable
from decimal import Decimal

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.engine.broker import Bar, executable_bar


def atr14(
    decision: Bar, history: Iterable[FeatureEvent], calendar: TradingCalendar
) -> Decimal | None:
    candidates = sorted(
        (
            row
            for row in history
            if row.session == "day"
            and (row.instrument_id, row.contract_month) == decision.key
            and row.bar_start < decision.feature.bar_start
            and row.available_at <= decision.feature.available_at
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
