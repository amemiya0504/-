from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI, SignalEvent
from gtts.engine.backtest import _Engine, run_backtest
from gtts.engine.events import OrderRequest, RollRequest, SettlementEvent, Side
from gtts.engine.portfolio import Portfolio
from gtts.risk.limits import RiskGate, RiskLimits
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.risk.stops import stop_from_distance, stop_loss_amount

D = Decimal
STOP_DISTANCE = D(10)
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
    *,
    month: str = MONTH,
    trade_date: date = TODAY,
    session: str = "day",
    price: int = 1000,
    high: int | None = None,
    low: int | None = None,
    close: int | None = None,
    volume: int = 20,
    available_at: datetime | None = None,
) -> FeatureEvent:
    close = price if close is None else close
    return FeatureEvent(
        instrument_id="MTX",
        contract_month=month,
        trade_date=trade_date,
        session=session,
        bar_start=start,
        bar_end=start + timedelta(minutes=5),
        available_at=available_at or start + timedelta(minutes=5),
        open_price_available_at=start,
        open_volume=volume,
        open_volume_available_at=start,
        values={
            "open": price,
            "high": max(price, close) + 1 if high is None else high,
            "low": min(price, close) - 1 if low is None else low,
            "close": close,
            "volume": volume,
        },
    )


def order(
    signal_id: str,
    at: datetime,
    *,
    month: str = MONTH,
    instrument_id: str = "MTX",
    strategy_id: str | None = None,
    side: Side = "buy",
    quantity: int = 1,
    stop_distance: Decimal | None = STOP_DISTANCE,
    exit_at: datetime | None = None,
) -> OrderRequest:
    return OrderRequest(
        signal=SignalEvent(
            signal_id=signal_id,
            strategy_id=strategy_id or signal_id,
            instrument_id=instrument_id,
            side=side,
            decision_at=at,
            available_at=at,
        ),
        contract_month=month,
        quantity=quantity,
        stop_distance=stop_distance,
        exit_at=exit_at,
    )


def test_risk_defaults_and_point_value_stop_calculation() -> None:
    limits = RiskLimits()

    assert limits.max_strategy_mtx == limits.max_portfolio_mtx == 1
    assert limits.max_trade_risk_fraction == D(".005")
    assert limits.max_daily_loss_fraction == D(".01")
    assert limits.margin_buffer_fraction == D(".2")
    assert stop_from_distance(D(1001), "buy", D(10), D(1)) == D(991)
    assert stop_from_distance(D(1001), "sell", D(10), D(1)) == D(1011)
    assert stop_loss_amount(D(1001), D(991), 1, 50) == D(500)
    assert stop_loss_amount(D(1001), D(991), 1, 200) == D(2000)


def test_entry_slippage_is_not_counted_twice_in_stop_budget(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        bars,
        orders=[order("slippage-budget", bars[0].available_at, stop_distance=D(8))],
        risk_limits=RiskLimits(),
    )

    assert [row.reason for row in result.fills] == ["signal"]


def test_late_prior_trade_date_snapshot_does_not_clear_daily_halt(schedules) -> None:
    gate = RiskGate(RiskLimits())
    snapshot = Portfolio(D(100000)).snapshot(schedules, datetime(2026, 12, 1, 9, 0, tzinfo=TAIPEI))
    current_trade_date = date(2026, 12, 2)

    assert not gate.observe(snapshot, current_trade_date)
    assert gate.observe(snapshot.model_copy(update={"equity": D(98000)}), current_trade_date)
    assert not gate.observe(snapshot.model_copy(update={"equity": D(110000)}), date(2026, 12, 1))
    assert gate.daily_halted
    assert gate.trade_date == current_trade_date


def test_old_tagged_snapshot_still_trips_active_daily_limit(schedules) -> None:
    gate = RiskGate(RiskLimits())
    snapshot = Portfolio(D(100000)).snapshot(schedules, datetime(2026, 12, 2, 9, 0, tzinfo=TAIPEI))
    active_trade_date = date(2026, 12, 2)

    assert not gate.observe(snapshot, active_trade_date)
    assert gate.observe(snapshot.model_copy(update={"equity": D(98000)}), date(2026, 12, 1))
    assert gate.trade_date == active_trade_date
    assert gate.daily_halted


def test_late_prior_day_feature_cannot_clear_current_night_loss_halt(calendar, schedules) -> None:
    expiry_day = date(2026, 12, 16)
    day_open = datetime(2026, 12, 16, 13, 15, tzinfo=TAIPEI)
    night_open = datetime(2026, 12, 16, 15, 0, tzinfo=TAIPEI)
    settlement_at = datetime(2026, 12, 16, 15, 3, tzinfo=TAIPEI)
    night_next_open = datetime(2026, 12, 16, 15, 5, tzinfo=TAIPEI)
    final_costs = schedules.costs["MTX"][0].model_copy(
        update={"final_settlement_fee_per_contract": D(0), "final_settlement_tax_rate": D(0)}
    )
    schedules = schedules.model_copy(update={"costs": {"MTX": (final_costs,)}})
    late_old_day = make_bar(
        datetime(2026, 12, 16, 13, 40, tzinfo=TAIPEI),
        month="202701",
        trade_date=expiry_day,
        price=940,
        low=930,
        high=1010,
        available_at=settlement_at,
    )
    night_202701 = make_bar(
        night_open,
        month="202701",
        trade_date=expiry_day + timedelta(days=1),
        session="night",
    )
    target_open = make_bar(
        night_next_open,
        month="202702",
        trade_date=expiry_day + timedelta(days=1),
        session="night",
    )
    final_settlement = SettlementEvent(
        instrument_id="MTX",
        contract_month="202612",
        trade_date=expiry_day,
        available_at=settlement_at,
        price=D(940),
        final=True,
    )

    result = run_backtest(
        calendar,
        schedules,
        D(200000),
        [
            make_bar(day_open, month="202612", trade_date=expiry_day),
            late_old_day,
            night_202701,
            target_open,
        ],
        orders=[
            order("expiry-position", day_open, month="202612", stop_distance=D(10)),
            order(
                "after-late-data",
                settlement_at,
                month="202702",
                stop_distance=D(2),
            ),
        ],
        settlements=[final_settlement],
        risk_limits=RiskLimits(),
    )

    assert [row.reason for row in result.fills] == ["signal"]
    assert len(result.settlements) == 1
    assert any(row.code == "daily_loss_halt" for row in result.audit)
    assert any(row.code == "daily_loss_limit" for row in result.audit)


def test_engine_trade_date_only_advances(calendar, schedules) -> None:
    engine = _Engine(calendar, schedules, D(100000), None, RiskLimits())
    engine.advance_trade_date(date(2026, 12, 2))
    engine.advance_trade_date(date(2026, 12, 1))

    assert engine.current_trade_date == date(2026, 12, 2)


@pytest.mark.parametrize(
    ("distance", "expected_code"),
    [(None, "missing_protective_stop"), (D(11), "trade_risk_limit")],
)
def test_pretrade_rejects_unprotected_or_overbudget_orders(
    calendar, schedules, distance: Decimal | None, expected_code: str
) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    request = order("risk", bars[0].available_at, stop_distance=distance)

    result = run_backtest(
        calendar, schedules, D(100000), bars, orders=[request], risk_limits=RiskLimits()
    )

    assert not result.fills
    assert [row.code for row in result.audit if row.code == expected_code] == [expected_code]


def test_same_direction_strategies_share_one_mtx_portfolio_slot(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start, month=month) for month in (MONTH, "202701")] + [
        make_bar(start + timedelta(minutes=5), month=month) for month in (MONTH, "202701")
    ]
    requests = [
        order("strategy-a", bars[0].available_at, month=MONTH, stop_distance=D(2)),
        order("strategy-b", bars[1].available_at, month="202701", stop_distance=D(2)),
    ]

    result = run_backtest(
        calendar, schedules, D(100000), bars, orders=requests, risk_limits=RiskLimits()
    )

    assert len(result.fills) == 1
    assert result.snapshots[-1].positions in (
        {f"MTX:{MONTH}": 1},
        {"MTX:202701": 1},
    )
    assert any(row.code == "portfolio_position_limit" for row in result.audit)


def test_each_strategy_limit_counts_held_contracts(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [
        make_bar(start + timedelta(minutes=5 * index), month=month)
        for index in range(3)
        for month in (MONTH, "202701")
    ]
    requests = [
        order(
            "same-strategy-a",
            bars[0].available_at,
            month=MONTH,
            strategy_id="shared",
            stop_distance=D(2),
        ),
        order(
            "same-strategy-b",
            bars[1].available_at,
            month="202701",
            strategy_id="shared",
            stop_distance=D(2),
        ),
    ]

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        bars,
        orders=requests,
        risk_limits=RiskLimits(max_portfolio_mtx=2),
    )

    assert len(result.fills) == 1
    assert any(row.code == "strategy_position_limit" for row in result.audit)


def test_cash_buffer_rejects_entry_after_initial_margin(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    no_slippage = schedules.model_copy(
        update={
            "costs": {"MTX": (schedules.costs["MTX"][0].model_copy(update={"slippage_ticks": 0}),)}
        }
    )

    result = run_backtest(
        calendar,
        no_slippage,
        D(14000),
        bars,
        orders=[order("buffer", bars[0].available_at, stop_distance=D(1))],
        risk_limits=RiskLimits(),
    )

    assert not result.fills
    assert any(row.code == "margin_cash_buffer" for row in result.audit)


def test_margin_increase_rejects_entry_when_cash_buffer_is_insufficient(
    calendar, schedules
) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    raised_margin = schedules.model_copy(
        update={
            "margins": {
                "MTX": (
                    schedules.margins["MTX"][0],
                    schedules.margins["MTX"][0].model_copy(
                        update={
                            "effective_from": start,
                            "initial": D(20000),
                            "maintenance": D(18000),
                        }
                    ),
                )
            },
            "costs": {"MTX": (schedules.costs["MTX"][0].model_copy(update={"slippage_ticks": 0}),)},
        }
    )

    result = run_backtest(
        calendar,
        raised_margin,
        D(23000),
        bars,
        orders=[order("raised-margin", bars[0].available_at, stop_distance=D(1))],
        risk_limits=RiskLimits(),
    )

    assert not result.fills
    assert any(row.code == "margin_cash_buffer" for row in result.audit)


def test_risk_gate_rejects_roll_without_destination_stop(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start, month=MONTH), make_bar(start + timedelta(minutes=5), month=MONTH)]
    request = RollRequest(
        signal=SignalEvent(
            signal_id="roll",
            strategy_id="roll",
            instrument_id="MTX",
            side="buy",
            decision_at=bars[0].available_at,
            available_at=bars[0].available_at,
        ),
        from_contract_month=MONTH,
        to_contract_month="202701",
        quantity=1,
    )

    result = run_backtest(
        calendar, schedules, D(100000), bars, orders=[request], risk_limits=RiskLimits()
    )

    assert not result.fills
    assert any(row.code == "risk_limited_roll" for row in result.audit)


def test_daily_loss_halt_blocks_next_entry_after_gap_stop(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [
        make_bar(start),
        make_bar(start + timedelta(minutes=5)),
        make_bar(
            start + timedelta(minutes=10),
            price=940,
            high=950,
            low=938,
            close=945,
            volume=20,
        ),
        make_bar(start + timedelta(minutes=15)),
    ]
    requests = [
        order("first", bars[0].available_at),
        order("second", bars[1].available_at, stop_distance=D(1)),
    ]

    result = run_backtest(
        calendar,
        schedules,
        D(200000),
        bars,
        orders=requests,
        settlements=[
            SettlementEvent(
                instrument_id="MTX",
                contract_month=MONTH,
                trade_date=TODAY + timedelta(days=100),
                available_at=bars[0].available_at,
                price=D(1000),
            )
        ],
        risk_limits=RiskLimits(),
    )

    assert [row.reason for row in result.fills] == ["signal", "stop"]
    assert result.fills[1].reference_price == D(940)
    assert result.snapshots[-1].equity < D(198000)
    assert any(row.code == "daily_loss_halt" for row in result.audit)
    assert any(row.code == "daily_loss_limit" for row in result.audit)
    assert any(row.code == "invalid_settlement" for row in result.audit)


def test_daily_loss_after_stop_costs_blocks_same_open_entry(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [
        make_bar(
            start + timedelta(minutes=5 * index),
            month=month,
            price=962 if index == 2 and month == MONTH else 1000,
        )
        for index in range(4)
        for month in (MONTH, "202701")
    ]
    requests = [
        order("first-stop", bars[0].available_at, month=MONTH, stop_distance=D(10)),
        order(
            "next-entry",
            next(
                row.available_at
                for row in bars
                if row.contract_month == "202701" and row.bar_start == start + timedelta(minutes=5)
            ),
            month="202701",
            stop_distance=D(1),
        ),
    ]

    result = run_backtest(
        calendar, schedules, D(200000), bars, orders=requests, risk_limits=RiskLimits()
    )

    assert [row.reason for row in result.fills] == ["signal", "stop"]
    assert any(row.code == "daily_loss_halt" for row in result.audit)
    assert any(row.code == "daily_loss_limit" for row in result.audit)


def test_missing_open_latches_data_kill_switch(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=10))]
    requests = [
        order("missed-open", bars[0].available_at, stop_distance=D(1)),
        order("after-interruption", bars[1].available_at, stop_distance=D(1)),
    ]

    result = run_backtest(
        calendar, schedules, D(100000), bars, orders=requests, risk_limits=RiskLimits()
    )

    assert not result.fills
    assert any(row.code == "missing_open" for row in result.audit)
    assert any(row.code == "data_kill_switch" for row in result.audit)


def test_gap_is_detected_before_recovery_open_entry(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    bars.extend([make_bar(start + timedelta(minutes=15)), make_bar(start + timedelta(minutes=20))])
    decision_at = start + timedelta(minutes=10, seconds=1)

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        bars,
        orders=[order("recovery-open", decision_at, stop_distance=D(1))],
        risk_limits=RiskLimits(),
    )

    assert not result.fills
    assert any(row.code == "data_kill_switch" for row in result.audit)


def test_gap_without_orders_still_records_data_kill_audit(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    bars.append(make_bar(start + timedelta(minutes=15)))

    result = run_backtest(calendar, schedules, D(100000), bars, risk_limits=RiskLimits())

    assert any(row.code == "data_kill_switch" for row in result.audit)


def test_invalid_first_opening_bar_latches_data_kill(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    invalid = make_bar(start).model_copy(update={"session": "night"})
    bars = [
        invalid,
        make_bar(start + timedelta(minutes=5)),
        make_bar(start + timedelta(minutes=10)),
    ]
    decision_at = start + timedelta(seconds=1)

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        bars,
        orders=[order("after-invalid-open", decision_at, stop_distance=D(2))],
        risk_limits=RiskLimits(),
    )

    assert not result.fills
    assert any(row.code == "closed_or_unlisted" for row in result.audit)
    assert any(row.code == "data_kill_switch" for row in result.audit)


def test_invalid_opening_does_not_extend_unlimited_low_level_replay(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(13, 35), TAIPEI)
    invalid_late = make_bar(start + timedelta(minutes=90))

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        [
            make_bar(start),
            invalid_late,
        ],
        orders=[order("low-level", start, stop_distance=None)],
    )

    assert [row.signal_id for row in result.fills] == ["low-level"]
    assert result.snapshots[-1].snapshot_at == start + timedelta(minutes=5)
    assert any(row.code == "closed_or_unlisted" for row in result.audit)
    assert not any(row.code == "missing_settlement" for row in result.audit)


def test_future_invalid_open_does_not_block_earlier_entry(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    invalid = make_bar(start + timedelta(minutes=10)).model_copy(update={"session": "night"})
    bars = [
        make_bar(start),
        make_bar(start + timedelta(minutes=5)),
        invalid,
        make_bar(start + timedelta(minutes=15)),
        make_bar(start + timedelta(minutes=20)),
    ]
    later_decision = start + timedelta(minutes=10, seconds=1)

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        bars,
        orders=[
            order("valid-past", start, stop_distance=D(2)),
            order("after-invalid", later_decision, stop_distance=D(2)),
        ],
        risk_limits=RiskLimits(),
    )

    assert [row.signal_id for row in result.fills] == ["valid-past"]
    data_halts = [row for row in result.audit if row.code == "data_kill_switch"]
    assert data_halts and all(row.at >= start + timedelta(minutes=10) for row in data_halts)


def test_invalid_stop_estimate_does_not_latch_market_data(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    requests = [
        order("bad-stop", bars[0].available_at, stop_distance=D(1000)),
        order("good-stop", bars[0].available_at, stop_distance=D(2)),
    ]

    result = run_backtest(
        calendar, schedules, D(100000), bars, orders=requests, risk_limits=RiskLimits()
    )

    assert [row.signal_id for row in result.fills] == ["good-stop"]
    assert any(row.code == "invalid_risk_estimate" for row in result.audit)
    assert not any(row.code == "data_kill_switch" for row in result.audit)


def test_missing_timed_exit_open_latches_data_but_allows_manual_flatten(
    calendar, schedules
) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [make_bar(start), make_bar(start + timedelta(minutes=5))]
    bars.extend([make_bar(start + timedelta(minutes=15)), make_bar(start + timedelta(minutes=20))])
    flatten_at = start + timedelta(minutes=10, seconds=1)
    new_entry_at = start + timedelta(minutes=15, seconds=1)
    flatten = OrderRequest(
        signal=SignalEvent(
            signal_id="manual-flatten",
            strategy_id="manual",
            instrument_id="MTX",
            side="sell",
            decision_at=flatten_at,
            available_at=flatten_at,
        ),
        contract_month=MONTH,
        quantity=1,
        reduce_only=True,
    )
    new_entry = order("after-flatten", new_entry_at, stop_distance=D(1))

    result = run_backtest(
        calendar,
        schedules,
        D(100000),
        bars,
        orders=[
            order(
                "timed",
                bars[0].available_at,
                stop_distance=D(2),
                exit_at=bars[0].available_at + timedelta(minutes=5),
            ),
            flatten,
            new_entry,
        ],
        risk_limits=RiskLimits(),
    )

    assert [row.signal_id for row in result.fills] == ["timed", "manual-flatten"]
    assert any(row.code == "time_exit_unresolved" for row in result.audit)
    assert any(row.code == "data_kill_switch" for row in result.audit)


def test_daily_loss_audit_is_account_level_for_tx_only(calendar, schedules) -> None:
    start = datetime.combine(TODAY, time(9), TAIPEI)
    bars = [
        make_bar(
            start + timedelta(minutes=5 * index), price=975 if index == 2 else 1000
        ).model_copy(update={"instrument_id": "TX"})
        for index in range(4)
    ]
    tx_schedules = schedules.model_copy(
        update={
            "costs": {"TX": schedules.costs["MTX"]},
            "margins": {"TX": schedules.margins["MTX"]},
        }
    )
    request = order("tx-stop", bars[0].available_at, instrument_id="TX", stop_distance=D(3))

    result = run_backtest(
        calendar, tx_schedules, D(200000), bars, orders=[request], risk_limits=RiskLimits()
    )

    assert result.fills and {row.instrument_id for row in result.fills} == {"TX"}
    assert any(
        row.code == "daily_loss_halt" and row.instrument_id == "PORTFOLIO" for row in result.audit
    )
