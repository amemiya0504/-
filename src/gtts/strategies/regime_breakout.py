"""Research-only two-state volatility-filtered Donchian breakout."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import SignalEvent, _taipei_datetime
from gtts.engine.backtest import run_backtest
from gtts.engine.broker import INTERVAL, CannotFill, executable_bar
from gtts.engine.events import BacktestResult, OrderRequest, Side
from gtts.features.volatility import breakout_features, donchian_channel
from gtts.risk.margin import Schedules

STOP_ATR = Decimal("1.5")
TARGET_ATR = Decimal(2)
VOLUME_Z_MIN = Decimal(1)


def breakout_order(
    feature: FeatureEvent,
    history: Iterable[FeatureEvent],
    calendar: TradingCalendar,
    *,
    require_high_volatility: bool = True,
    decision_at: datetime | None = None,
) -> OrderRequest | None:
    """Return a next-open order only for a valid day-session close breakout."""
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
        if channel is None or channel[1] <= bar.close <= channel[0]:
            return None
        inputs = breakout_features(
            feature,
            rows,
            calendar,
            decision_at=at,
            include_regime=require_high_volatility,
        )
    except (CalendarGap, CannotFill, ValueError):
        return None
    if inputs is None:
        return None
    if require_high_volatility and (
        not inputs.high_volatility
        or inputs.volume_zscore is None
        or inputs.volume_zscore < VOLUME_Z_MIN
    ):
        return None
    side: Side | None = (
        "buy"
        if bar.close > inputs.donchian_high
        else "sell"
        if bar.close < inputs.donchian_low
        else None
    )
    if side is None:
        return None
    signal = SignalEvent(
        signal_id=f"s3:{feature.instrument_id}:{feature.contract_month}:{feature.bar_start.isoformat()}",
        strategy_id="s3_regime_breakout" if require_high_volatility else "s3_donchian_baseline",
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


def _run_policy(
    bars: list[FeatureEvent],
    calendar: TradingCalendar,
    schedules: Schedules,
    initial_cash: Decimal,
    before: datetime | None,
    *,
    require_high_volatility: bool,
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
        order = breakout_order(
            feature,
            history,
            calendar,
            require_high_volatility=require_high_volatility,
        )
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
    )


def _evaluable_breakouts(
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
        channel = donchian_channel(feature, rows, calendar)
        if channel is None:
            continue
        try:
            bar = executable_bar(feature, calendar)
        except CannotFill:
            continue
        if channel[1] <= bar.close <= channel[0]:
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
    """Compare the high-volatility state with the same unfiltered Donchian policy."""
    rows = list(bars)
    if before is not None:
        before = _taipei_datetime(before)
        rows = [row for row in rows if row.bar_start < before]
    evaluable = _evaluable_breakouts(rows, calendar, before)
    return BreakoutComparison(
        s3=_run_policy(
            rows,
            calendar,
            schedules,
            initial_cash,
            before,
            require_high_volatility=True,
            evaluable=evaluable,
        ),
        donchian_baseline=_run_policy(
            rows,
            calendar,
            schedules,
            initial_cash,
            before,
            require_high_volatility=False,
            evaluable=evaluable,
        ),
    )
