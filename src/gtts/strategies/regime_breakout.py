"""Research-only two-state volatility-filtered Donchian breakout and its baselines."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.quality import FeatureEvent, features_at
from gtts.data.schema import SignalEvent, _taipei_datetime
from gtts.engine.backtest import run_backtest
from gtts.engine.broker import INTERVAL, CannotFill, executable_bar
from gtts.engine.events import BacktestResult, OrderRequest, Side
from gtts.features.volatility import breakout_features, donchian_channel
from gtts.risk.limits import RiskLimits
from gtts.risk.margin import Schedules

STOP_ATR = Decimal("1.5")
TARGET_ATR = Decimal(2)
VOLUME_Z_MIN = Decimal(1)
ATR_TRIGGER = Decimal(1)

Policy = Literal["s3", "donchian", "atr"]
_STRATEGY_IDS: dict[Policy, str] = {
    "s3": "s3_regime_breakout",
    "donchian": "s3_donchian_baseline",
    "atr": "s3_atr_baseline",
}


def _previous_close(
    feature: FeatureEvent, rows: list[FeatureEvent], calendar: TradingCalendar, at: datetime
) -> Decimal | None:
    raw = [
        row
        for row in rows
        if row.instrument_id == feature.instrument_id
        and row.contract_month == feature.contract_month
        and row.trade_date == feature.trade_date
        and row.session == "day"
        and row.bar_end == feature.bar_start
        and row.available_at <= at
    ]
    previous = features_at(raw, at, calendar) if raw else []
    return executable_bar(previous[0], calendar).close if len(previous) == 1 else None


def breakout_order(
    feature: FeatureEvent,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    *,
    require_high_volatility: bool = True,
    decision_at: datetime | None = None,
) -> OrderRequest | None:
    """Return a next-open order only for a valid day-session close breakout."""
    policy: Policy = "s3" if require_high_volatility else "donchian"
    return _order(feature, history, calendar, policy, decision_at)


def atr_breakout_order(
    feature: FeatureEvent,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    *,
    decision_at: datetime | None = None,
) -> OrderRequest | None:
    """Baseline: close moves more than one ATR14 from the previous complete bar close."""
    return _order(feature, history, calendar, "atr", decision_at)


def _order(
    feature: FeatureEvent,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    policy: Policy,
    decision_at: datetime | None,
) -> OrderRequest | None:
    at = _taipei_datetime(decision_at or feature.available_at)
    if feature.instrument_id != "MTX" or not feature.contract_month:
        return None
    rows = list(history)
    try:
        bar = executable_bar(feature, calendar)
        session = calendar.day_session(
            feature.instrument_id, feature.trade_date, feature.contract_month
        )
        if (
            session is None
            or feature.session != "day"
            or feature.available_at != at
            or not bar.total_volume
            or at >= session.closes_at - INTERVAL
            or calendar.is_last_trade_date(
                feature.instrument_id, feature.contract_month, feature.trade_date
            )
        ):
            return None
        channel = donchian_channel(feature, rows, calendar, decision_at=at)
        # The 12-bar channel also gates the ATR baseline so all policies share one warm-up.
        if channel is None or (policy != "atr" and channel[1] <= bar.close <= channel[0]):
            return None
        inputs = breakout_features(
            feature,
            rows,
            calendar,
            decision_at=at,
            include_regime=policy == "s3",
        )
        reference = _previous_close(feature, rows, calendar, at) if policy == "atr" else None
    except (CalendarGap, CannotFill, ValueError):
        return None
    if inputs is None:
        return None
    if policy == "s3" and (
        not inputs.high_volatility
        or inputs.volume_zscore is None
        or inputs.volume_zscore < VOLUME_Z_MIN
    ):
        return None
    if policy == "atr":
        if reference is None:
            return None
        upper = reference + inputs.atr * ATR_TRIGGER
        lower = reference - inputs.atr * ATR_TRIGGER
    else:
        upper, lower = inputs.donchian_high, inputs.donchian_low
    side: Side | None = "buy" if bar.close > upper else "sell" if bar.close < lower else None
    if side is None:
        return None
    signal = SignalEvent(
        signal_id=f"s3:{feature.instrument_id}:{feature.contract_month}:{feature.bar_start.isoformat()}",
        strategy_id=_STRATEGY_IDS[policy],
        instrument_id=feature.instrument_id,
        side=side,
        decision_at=at,
        available_at=feature.available_at,
    )
    return OrderRequest(
        signal=signal,
        contract_month=feature.contract_month,
        quantity=1,
        max_instrument_exposure=1,
        stop_distance=inputs.atr * STOP_ATR,
        target_distance=inputs.atr * TARGET_ATR,
        exit_at=session.closes_at - INTERVAL,
    )


@dataclass(frozen=True)
class BreakoutComparison:
    s3: BacktestResult
    donchian_baseline: BacktestResult
    atr_baseline: BacktestResult


def _run_policy(
    bars: list[FeatureEvent],
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    before: datetime | None,
    *,
    policy: Policy,
    evaluable: set[tuple[str, str, datetime]],
) -> BacktestResult:
    history = list(bars)

    def decide(
        feature: FeatureEvent, positions: Mapping[tuple[str, str], int]
    ) -> tuple[OrderRequest, ...]:
        if (
            feature.instrument_id,
            feature.contract_month or "",
            feature.bar_start,
        ) not in evaluable:
            return ()
        if any(
            name == feature.instrument_id and quantity for (name, _), quantity in positions.items()
        ):
            return ()
        order = _order(feature, history, calendar, policy, None)
        if order is None:
            return ()
        return (order,)

    return run_backtest(
        calendar,
        schedules,
        initial_cash,
        bars,
        before=before,
        on_feature_with_positions=decide,
        risk_limits=RiskLimits(),
    )


def _evaluable_bars(
    rows: list[FeatureEvent], calendar: TradingCalendar, before: datetime | None
) -> set[tuple[str, str, datetime]]:
    result: set[tuple[str, str, datetime]] = set()
    for feature in rows:
        if (
            feature.instrument_id != "MTX"
            or feature.session != "day"
            or (before is not None and feature.available_at >= before)
        ):
            continue
        if donchian_channel(feature, rows, calendar) is None:
            continue
        inputs = breakout_features(feature, rows, calendar)
        if inputs is not None and inputs.volume_zscore is not None:
            result.add((feature.instrument_id, feature.contract_month or "", feature.bar_start))
    return result


def compare_regime_breakout(
    bars: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    *,
    before: datetime | None = None,
) -> BreakoutComparison:
    """Compare S3 with unfiltered Donchian and ATR breakouts on bars where S3 is computable."""
    rows = list(bars)
    if before is not None:
        before = _taipei_datetime(before)
        rows = [row for row in rows if row.bar_start < before]
    evaluable = _evaluable_bars(rows, calendar, before)
    results = {
        policy: _run_policy(
            rows, calendar, schedules, initial_cash, before, policy=policy, evaluable=evaluable
        )
        for policy in _STRATEGY_IDS
    }
    return BreakoutComparison(
        s3=results["s3"], donchian_baseline=results["donchian"], atr_baseline=results["atr"]
    )
