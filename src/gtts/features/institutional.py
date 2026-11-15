"""Point-in-time TX institutional open-interest features."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from math import isfinite
from statistics import fmean, pstdev

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.quality import point_in_time_join
from gtts.data.schema import _taipei_datetime
from gtts.data.sources import Observation
from gtts.data.sources.taifex import TaifexInstitutional

PARTICIPANTS = ("外資及陸資", "投信", "自營商")
Scores = tuple[float, float, float]


@dataclass(frozen=True)
class InstitutionalFeature:
    source_date: date
    decision_at: datetime
    available_at: datetime
    scores: Scores
    foreign_change: Decimal
    source_rows: tuple[Observation, ...]


def institutional_feature(
    observations: Iterable[Observation],
    calendar: TradingCalendar,
    *,
    source_date: date,
    decision_at: datetime,
    lookback: int = 60,
    allow_early_publication: bool = False,
) -> InstitutionalFeature | None:
    """Normalize the current OI change against earlier completed trade dates."""
    _taipei_datetime(decision_at)
    if lookback < 2:
        raise ValueError("institutional lookback must be at least two trading days")
    try:
        if not calendar.is_trade_date(source_date):
            return None
        days = [source_date]
        for _ in range(lookback + 1):
            days.append(calendar.previous_trade_date(days[-1]))
        days.reverse()
        gates: dict[date, datetime] = {}
        for day in days:
            session = calendar.day_session("TX", calendar.next_trade_date(day))
            if session is None:
                return None
            gates[day] = session.opens_at
    except CalendarGap:
        return None

    candidates = []
    for row in observations:
        if (
            row.source_id != TaifexInstitutional.source_id
            or row.instrument_id != "TX"
            or row.contract_month is not None
            or row.session is not None
            or row.participant not in PARTICIPANTS
            or row.trade_date not in gates
        ):
            continue
        available_at = row.available_at
        if not allow_early_publication or row.published_at is None or row.availability_estimated:
            available_at = max(available_at, gates[row.trade_date])
        if available_at <= decision_at:
            candidates.append(row)
    # Rank versions by the original release/ingestion times after the conservative gate.
    selected = point_in_time_join(candidates, decision_at, calendar)
    indexed = {(row.trade_date, row.participant): row for row in selected}
    if len(indexed) != len(selected):
        raise ValueError("multiple institutional observations per participant and trading day")

    def changes(participant: str) -> list[Decimal] | None:
        levels: list[Decimal] = []
        for day in days:
            row = indexed.get((day, participant))
            net = row.values.get("open_interest_net") if row is not None else None
            if net is None or not Decimal(net).is_finite() or net != int(net):
                return None
            levels.append(Decimal(net))
        return [current - previous for previous, current in pairwise(levels)]

    scores: list[float] = []
    last_changes: list[Decimal] = []
    for participant in PARTICIPANTS:
        series = changes(participant)
        if series is None:
            return None
        history = [float(value) for value in series[:-1]]
        current = float(series[-1])
        if not isfinite(current) or not all(map(isfinite, history)):
            return None
        spread = pstdev(history)
        # A constant window carries no standardized signal.
        score = (current - fmean(history)) / spread if spread else 0.0
        if not isfinite(score):
            return None
        scores.append(score)
        last_changes.append(series[-1])

    available_at = max(
        row.available_at
        if allow_early_publication
        and row.published_at is not None
        and not row.availability_estimated
        else max(row.available_at, gates[row.trade_date])
        for row in selected
    )
    return InstitutionalFeature(
        source_date,
        decision_at,
        available_at,
        (scores[0], scores[1], scores[2]),
        last_changes[0],
        tuple(selected),
    )
