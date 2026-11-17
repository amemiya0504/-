from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.engine.backtest import run_backtest
from gtts.engine.broker import executable_bar
from gtts.features.volatility import atr14, breakout_features, donchian_channel
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.strategies.regime_breakout import (
    atr_breakout_order,
    breakout_order,
    compare_regime_breakout,
)

D = Decimal
MONTH = "202612"
TODAY = date(2026, 12, 1)


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


@pytest.fixture
def schedules() -> Schedules:
    return Schedules(
        costs={
            "MTX": (
                Costs(
                    effective_from=date(2026, 1, 1),
                    broker_fee_per_contract=D(2),
                    tax_rate=D(".00002"),
                    slippage_ticks=1,
                ),
            )
        },
        margins={
            "MTX": (
                Margin(
                    effective_from=datetime(2026, 1, 1, tzinfo=TAIPEI),
                    initial=D(12000),
                    maintenance=D(10000),
                ),
            )
        },
    )


def make_bar(
    start: datetime,
    trade_date: date,
    *,
    price: int = 1000,
    high: int | None = None,
    low: int | None = None,
    close: int | None = None,
    volume: int = 10,
    opening_volume: int | None = None,
) -> FeatureEvent:
    close = price if close is None else close
    return FeatureEvent(
        instrument_id="MTX",
        contract_month=MONTH,
        trade_date=trade_date,
        session="day",
        bar_start=start,
        bar_end=start + timedelta(minutes=5),
        available_at=start + timedelta(minutes=5),
        open_price_available_at=start,
        open_volume=volume if opening_volume is None else opening_volume,
        open_volume_available_at=start,
        values={
            "open": price,
            "high": max(price, close) + 1 if high is None else high,
            "low": min(price, close) - 1 if low is None else low,
            "close": close,
            "volume": volume,
        },
    )


def sample_data(
    calendar: TradingCalendar,
    *,
    trade_date: date = TODAY,
    last_spread: int | None = None,
    current_volume: int = 40,
) -> tuple[list[FeatureEvent], FeatureEvent]:
    dates = []
    day = trade_date
    for _ in range(20):
        day = calendar.previous_trade_date(day)
        dates.append(day)
    history: list[FeatureEvent] = []
    for index, past_day in enumerate(reversed(dates)):
        session = calendar.day_session("MTX", past_day, MONTH)
        assert session is not None
        spread = 2 + index
        if last_spread is not None and index == 19:
            spread = last_spread
        volume = 8 + index % 4
        start = session.opens_at
        while start + timedelta(minutes=5) <= session.closes_at:
            history.append(
                make_bar(
                    start,
                    past_day,
                    high=1000 + spread,
                    low=1000 - spread,
                    volume=volume,
                )
            )
            start += timedelta(minutes=5)

    session = calendar.day_session("MTX", trade_date, MONTH)
    assert session is not None
    current_start = datetime.combine(trade_date, time(10, 0), TAIPEI)
    start = session.opens_at
    while start < current_start:
        history.append(make_bar(start, trade_date, volume=10))
        start += timedelta(minutes=5)
    current = make_bar(
        current_start,
        trade_date,
        price=1002,
        high=1007,
        low=1000,
        close=1006,
        volume=current_volume,
    )
    return history, current


def test_s3_order_uses_high_volatility_volume_breakout_and_next_open(calendar) -> None:
    history, current = sample_data(calendar)
    inputs = breakout_features(current, history, calendar)
    assert inputs is not None
    assert inputs.high_volatility and inputs.volume_zscore >= 1

    request = breakout_order(current, history, calendar)
    assert request is not None
    assert request.signal.side == "buy"
    assert request.signal.decision_at == current.bar_end
    assert request.signal.available_at == current.available_at
    assert request.stop_distance == inputs.atr * D("1.5")
    assert request.target_distance == inputs.atr * D(2)
    assert request.exit_at == datetime(2026, 12, 1, 13, 40, tzinfo=TAIPEI)


def test_baseline_omits_only_the_volatility_state_filter(calendar) -> None:
    history, current = sample_data(calendar, last_spread=2, current_volume=10)
    inputs = breakout_features(current, history, calendar)
    assert inputs is not None and not inputs.high_volatility
    assert inputs.volume_zscore is not None and inputs.volume_zscore < 1
    assert breakout_order(current, history, calendar) is None
    baseline = breakout_order(current, history, calendar, require_high_volatility=False)
    assert baseline is not None
    assert baseline.stop_distance == inputs.atr * D("1.5")
    assert baseline.target_distance == inputs.atr * D(2)


def test_rejects_fewer_than_twelve_prior_bars(calendar) -> None:
    history, current = sample_data(calendar)
    current_day = [row for row in history if row.trade_date == TODAY]
    assert donchian_channel(current, current_day[-11:], calendar) is None
    assert donchian_channel(current, current_day[-12:], calendar) is not None


def test_baseline_needs_no_twenty_day_regime_history(calendar) -> None:
    history, current = sample_data(calendar)
    current_day = [row for row in history if row.trade_date == TODAY]
    baseline = breakout_order(current, current_day, calendar, require_high_volatility=False)
    assert baseline is not None
    assert breakout_order(current, current_day, calendar) is None


def test_rejects_unfinished_or_missing_volume_bar(calendar) -> None:
    history, current = sample_data(calendar)
    assert (
        breakout_features(
            current,
            history,
            calendar,
            decision_at=current.bar_end - timedelta(seconds=1),
        )
        is None
    )
    missing_volume = current.model_copy(
        update={"values": {key: value for key, value in current.values.items() if key != "volume"}}
    )
    assert breakout_order(missing_volume, history, calendar) is None
    no_volume = current.model_copy(update={"values": {**current.values, "volume": 0}})
    assert breakout_order(no_volume, history, calendar) is None


def test_incomplete_historical_session_fails_closed(calendar) -> None:
    history, current = sample_data(calendar)
    del history[0]
    assert breakout_order(current, history, calendar) is None


def test_night_and_wrong_day_rows_do_not_pollute_features(calendar) -> None:
    history, current = sample_data(calendar)
    before = breakout_features(current, history, calendar)
    assert before is not None
    history.extend(
        [
            current.model_copy(
                update={
                    "session": "night",
                    "trade_date": date(2026, 12, 2),
                    "bar_start": datetime(2026, 12, 1, 15, tzinfo=TAIPEI),
                    "bar_end": datetime(2026, 12, 1, 15, 5, tzinfo=TAIPEI),
                }
            ),
            current.model_copy(
                update={"trade_date": date(2026, 11, 30), "bar_start": current.bar_start}
            ),
        ]
    )
    assert breakout_features(current, history, calendar) == before


def test_halted_zero_volume_price_does_not_expand_daily_volatility(calendar) -> None:
    history, current = sample_data(calendar)
    before = breakout_features(current, history, calendar)
    assert before is not None
    previous_day = max(row.trade_date for row in history if row.trade_date < TODAY)
    first_bar = min(
        (index for index, row in enumerate(history) if row.trade_date == previous_day),
        key=lambda index: history[index].bar_start,
    )
    halted = history[first_bar]
    history[first_bar] = halted.model_copy(
        update={
            "open_volume": 0,
            "values": {
                **halted.values,
                "high": 2000,
                "volume": 0,
                "halted": 1,
            },
        }
    )

    after = breakout_features(current, history, calendar)
    assert after is not None
    assert after.volatility_percentile == before.volatility_percentile
    assert after.high_volatility == before.high_volatility


def test_unrelated_taiex_rows_do_not_invalidate_mtx_regime(calendar) -> None:
    history, current = sample_data(calendar)
    before = breakout_features(current, history, calendar)
    assert before is not None
    first = history[0]
    history.append(
        FeatureEvent(
            instrument_id="TAIEX",
            trade_date=first.trade_date,
            session="day",
            bar_start=first.bar_start,
            bar_end=first.bar_end,
            available_at=first.available_at,
            values={"close": 10000},
        )
    )

    assert breakout_features(current, history, calendar) == before


def test_breakout_atr_uses_explicit_delayed_decision_cutoff(calendar) -> None:
    history, current = sample_data(calendar)
    late_at = current.available_at + timedelta(minutes=1)
    late = history[-1].model_copy(
        update={
            "available_at": late_at,
            "values": {**history[-1].values, "high": 1003, "low": 997, "close": 1001},
        }
    )
    history[-1] = late

    inputs = breakout_features(current, history, calendar, decision_at=late_at)
    current_bar = executable_bar(current, calendar)
    atr_by_cutoff = atr14(current_bar, history, calendar, decision_at=late_at)
    atr_at_bar_availability = atr14(current_bar, history, calendar)

    assert inputs is not None and atr_by_cutoff is not None
    assert inputs.atr == atr_by_cutoff
    assert atr_at_bar_availability is None


def test_atr_rejects_signal_bar_unavailable_at_decision_cutoff(calendar) -> None:
    history, current = sample_data(calendar)
    decision_at = current.bar_end - timedelta(seconds=1)

    assert (
        atr14(executable_bar(current, calendar), history, calendar, decision_at=decision_at) is None
    )


def test_expiry_session_never_creates_entry(calendar) -> None:
    expiry = calendar.last_trade_date("MTX", MONTH)
    history, current = sample_data(calendar, trade_date=expiry)
    assert current.bar_start.date() == expiry
    assert breakout_order(current, history, calendar) is None


def test_strategy_is_fixed_to_one_mini_contract(calendar) -> None:
    history, current = sample_data(calendar)
    tx_bar = current.model_copy(update={"instrument_id": "TX"})
    assert breakout_order(tx_bar, history, calendar) is None


def test_strategy_replay_gap_stop_fills_at_gap_open(calendar, schedules) -> None:
    history, current = sample_data(calendar)
    request = breakout_order(current, history, calendar)
    assert request is not None
    entry_bar = make_bar(
        current.bar_end,
        TODAY,
        price=1007,
        high=1008,
        low=1006,
        close=1007,
        volume=20,
        opening_volume=1,
    )
    gap_bar = make_bar(
        entry_bar.bar_end,
        TODAY,
        price=990,
        high=993,
        low=990,
        close=991,
        volume=20,
        opening_volume=1,
    )
    result = run_backtest(
        calendar, schedules, D(100000), [current, entry_bar, gap_bar], orders=[request]
    )
    assert [(row.reason, row.reference_price) for row in result.fills] == [
        ("signal", D(1007)),
        ("stop", D(990)),
    ]


def test_strategy_replay_same_bar_stop_target_conflict_prefers_stop(calendar, schedules) -> None:
    history, current = sample_data(calendar)
    request = breakout_order(current, history, calendar)
    assert request is not None
    conflict = make_bar(
        current.bar_end,
        TODAY,
        price=1007,
        high=1020,
        low=990,
        close=1010,
        volume=20,
        opening_volume=1,
    )
    result = run_backtest(calendar, schedules, D(100000), [current, conflict], orders=[request])
    assert [row.reason for row in result.fills] == ["signal", "stop"]
    assert result.fills[1].reference_price == D(1005)


def test_last_executable_open_signal_cannot_enter_after_daily_exit(calendar) -> None:
    history, _ = sample_data(calendar)
    session = calendar.day_session("MTX", TODAY, MONTH)
    assert session is not None
    start = datetime.combine(TODAY, time(10, 0), TAIPEI)
    while start < datetime(2026, 12, 1, 13, 35, tzinfo=TAIPEI):
        history.append(make_bar(start, TODAY))
        start += timedelta(minutes=5)
    last_bar = make_bar(
        datetime(2026, 12, 1, 13, 35, tzinfo=TAIPEI),
        TODAY,
        price=1002,
        high=1007,
        low=1000,
        close=1006,
        volume=40,
    )
    assert last_bar.available_at == session.closes_at - timedelta(minutes=5)
    assert breakout_order(last_bar, history, calendar) is None


def test_same_cost_baseline_replay_has_separate_result(calendar, schedules) -> None:
    history, current = sample_data(calendar)
    following = make_bar(
        current.bar_end,
        TODAY,
        price=1007,
        high=1010,
        low=1005,
        close=1008,
        volume=20,
    )
    rows = [*history, current, following]
    comparison = compare_regime_breakout(
        rows, calendar, schedules, D(100000), before=following.bar_end + timedelta(seconds=1)
    )
    assert comparison.s3.orders
    assert comparison.donchian_baseline.orders
    assert comparison.atr_baseline.orders
    for result in (comparison.s3, comparison.donchian_baseline, comparison.atr_baseline):
        order = result.orders[0]
        assert order.order_at == current.available_at
        assert order.side == "buy"
        assert result.audit == ()


def test_cutoff_keeps_open_only_evidence_for_late_complete_bar(calendar, schedules) -> None:
    history, current = sample_data(calendar)
    next_open = make_bar(
        current.bar_end,
        TODAY,
        price=1007,
        high=1008,
        low=1006,
        close=1007,
        volume=20,
        opening_volume=1,
    )
    cutoff = next_open.bar_start + timedelta(microseconds=2)
    comparison = compare_regime_breakout(
        [*history, current, next_open],
        calendar,
        schedules,
        D(100000),
        before=cutoff,
    )

    for result in (comparison.s3, comparison.donchian_baseline, comparison.atr_baseline):
        assert result.fills
        assert result.fills[0].fill_at == next_open.bar_start + timedelta(microseconds=1)


def test_retries_after_rejected_open_on_later_breakout(calendar, schedules) -> None:
    history, current = sample_data(calendar)
    rejected_open = make_bar(
        current.bar_end,
        TODAY,
        price=1007,
        high=1011,
        low=1006,
        close=1010,
        volume=20,
        opening_volume=0,
    )
    next_open = make_bar(
        rejected_open.bar_end,
        TODAY,
        price=1012,
        high=1013,
        low=1011,
        close=1012,
        volume=20,
        opening_volume=1,
    )
    comparison = compare_regime_breakout(
        [*history, current, rejected_open, next_open],
        calendar,
        schedules,
        D(100000),
        before=next_open.bar_start + timedelta(microseconds=2),
    )

    for result in (comparison.s3, comparison.donchian_baseline, comparison.atr_baseline):
        assert len(result.orders) == 2
        assert [row.code for row in result.audit] == ["insufficient_volume"]
        assert len(result.fills) == 1
        assert result.fills[0].signal_id == (
            f"s3:MTX:{MONTH}:{rejected_open.bar_start.isoformat()}"
        )


def test_atr_baseline_enters_on_one_atr_close_move(calendar) -> None:
    history, current = sample_data(calendar, last_spread=2, current_volume=10)
    inputs = breakout_features(current, history, calendar)
    assert inputs is not None and not inputs.high_volatility

    request = atr_breakout_order(current, history, calendar)

    assert request is not None
    assert request.signal.side == "buy"
    assert request.signal.strategy_id == "s3_atr_baseline"
    assert request.stop_distance == inputs.atr * D("1.5")
    assert request.target_distance == inputs.atr * D(2)


def test_atr_baseline_ignores_donchian_breakout_below_one_atr(calendar) -> None:
    history, current = sample_data(calendar)
    small = current.model_copy(
        update={
            "values": {**current.values, "open": 1000, "high": 1003, "low": 1000, "close": 1002}
        }
    )
    assert breakout_order(small, history, calendar, require_high_volatility=False) is not None
    assert atr_breakout_order(small, history, calendar) is None
    assert atr_breakout_order(current, history[:-1], calendar) is None


def test_atr_baseline_threshold_is_strict(calendar) -> None:
    history, current = sample_data(calendar, last_spread=2, current_volume=10)

    buy_at_boundary = current.model_copy(
        update={
            "values": {**current.values, "open": 1000, "high": 1002, "low": 1000, "close": 1002}
        }
    )
    buy_inputs = breakout_features(buy_at_boundary, history, calendar)
    assert buy_inputs is not None and buy_inputs.atr == D(2)
    assert atr_breakout_order(buy_at_boundary, history, calendar) is None

    buy_beyond = buy_at_boundary.model_copy(
        update={"values": {**buy_at_boundary.values, "high": 1003, "close": 1003}}
    )
    buy_inputs = breakout_features(buy_beyond, history, calendar)
    assert buy_inputs is not None and buy_inputs.atr < D(3)
    buy = atr_breakout_order(buy_beyond, history, calendar)
    assert buy is not None and buy.signal.side == "buy"

    sell_at_boundary = current.model_copy(
        update={"values": {**current.values, "open": 1000, "high": 1000, "low": 998, "close": 998}}
    )
    sell_inputs = breakout_features(sell_at_boundary, history, calendar)
    assert sell_inputs is not None and sell_inputs.atr == D(2)
    assert atr_breakout_order(sell_at_boundary, history, calendar) is None

    sell_beyond = sell_at_boundary.model_copy(
        update={"values": {**sell_at_boundary.values, "low": 997, "close": 997}}
    )
    sell_inputs = breakout_features(sell_beyond, history, calendar)
    assert sell_inputs is not None and sell_inputs.atr < D(3)
    sell = atr_breakout_order(sell_beyond, history, calendar)
    assert sell is not None and sell.signal.side == "sell"


def test_atr_baseline_ignores_late_revision_of_previous_bar(calendar) -> None:
    history, current = sample_data(calendar)
    expected = atr_breakout_order(current, history, calendar)
    assert expected is not None

    previous = history[-1]
    revised = previous.model_copy(
        update={
            "available_at": current.available_at + timedelta(minutes=1),
            "values": {**previous.values, "high": 1007, "low": 1005, "close": 1006},
        }
    )
    assert (revised.bar_start, revised.bar_end) == (previous.bar_start, previous.bar_end)
    assert atr_breakout_order(current, [*history, revised], calendar) == expected
