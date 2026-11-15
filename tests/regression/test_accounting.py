from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI, SignalEvent
from gtts.engine.backtest import run_backtest
from gtts.engine.events import OrderRequest, RollRequest, SettlementEvent, Side
from gtts.risk.margin import Costs, Margin, Schedules

D = Decimal
NOV = "202611"
DEC = "202612"
DAY = date(2026, 11, 13)


def at(hour: int, minute: int, day: int = 13) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TAIPEI)


def bar(
    start: datetime,
    price: int,
    *,
    month: str = NOV,
    low: int | None = None,
    high: int | None = None,
    close: int | None = None,
    volume: int = 10,
    opening_volume: int | None = None,
    trade_date: date | None = None,
    night: bool = False,
    delay: int = 0,
) -> FeatureEvent:
    end = start + timedelta(minutes=5)
    return FeatureEvent(
        instrument_id="TX",
        contract_month=month,
        trade_date=trade_date or start.date(),
        session="night" if night else "day",
        bar_start=start,
        bar_end=end,
        available_at=end + timedelta(minutes=delay),
        open_price_available_at=start,
        open_volume=volume if opening_volume is None else opening_volume,
        open_volume_available_at=start,
        values={
            "open": price,
            "high": high or price,
            "low": low or price,
            "close": close or price,
            "volume": volume,
        },
    )


def without_open_volume(row: FeatureEvent) -> FeatureEvent:
    return row.model_copy(update={"open_volume": None, "open_volume_available_at": None})


def signal(tag: str, decision: datetime, side: Side) -> SignalEvent:
    return SignalEvent(
        signal_id=tag,
        strategy_id="golden",
        instrument_id="TX",
        side=side,
        decision_at=decision,
        available_at=decision,
    )


def order(
    tag: str,
    decision: datetime,
    side: Side,
    *,
    quantity: int = 1,
    month: str = NOV,
    stop: int | None = None,
    stop_distance: Decimal | None = None,
    target: int | None = None,
    target_distance: Decimal | None = None,
    exit_at: datetime | None = None,
    reduce_only: bool = False,
    max_instrument_exposure: int | None = None,
) -> OrderRequest:
    return OrderRequest(
        signal=signal(tag, decision, side),
        contract_month=month,
        quantity=quantity,
        stop_price=stop,
        stop_distance=stop_distance,
        target_price=target,
        target_distance=target_distance,
        exit_at=exit_at,
        reduce_only=reduce_only,
        max_instrument_exposure=max_instrument_exposure,
    )


def test_timed_exit_requires_a_filled_entry_and_uses_same_session_open(calendar, schedules) -> None:
    entry = bar(at(9, 5), 1000)
    close = bar(at(13, 40), 1020, low=1010, close=1010)
    request = order("timed", at(9, 5), "buy", exit_at=at(13, 40))
    result = run_backtest(calendar, schedules, D(10000), [entry, close], orders=[request])
    assert [(fill.reason, fill.reference_price) for fill in result.fills] == [
        ("signal", D(1000)),
        ("time_exit", D(1020)),
    ]
    assert result.fills[1].fill_at == at(13, 40) + timedelta(microseconds=1)
    assert not result.snapshots[-1].positions
    rejected = run_backtest(
        calendar, schedules, D(10000), [bar(at(9, 5), 1000, volume=0), close], orders=[request]
    )
    assert not rejected.fills
    assert [audit.code for audit in rejected.audit] == ["insufficient_volume"]


def test_total_bar_volume_cannot_authorize_exact_opening_fills(calendar, schedules) -> None:
    request = order("opening", at(9, 5), "buy", quantity=2)
    for volume in (1, 2):
        no_open = without_open_volume(bar(at(9, 5), 1000, volume=volume))
        result = run_backtest(calendar, schedules, D(10000), [no_open], orders=[request])
        assert not result.fills
        assert [row.code for row in result.audit] == ["missing_open_volume"]

    entry = bar(at(9, 5), 1000, volume=2)
    data = entry.model_dump()
    data["open_volume_available_at"] = entry.bar_end
    with pytest.raises(ValidationError, match="available by the five-minute bar start"):
        FeatureEvent.model_validate(data)
    late = entry.model_copy(update={"open_volume_available_at": entry.bar_end})
    no_timestamp = entry.model_copy(update={"open_volume_available_at": None})
    no_volume = entry.model_copy(update={"open_volume": None})
    implicit = without_open_volume(entry).model_copy(
        update={"values": {**entry.values, "open_volume": 2}}
    )
    for invalid in (late, no_timestamp, no_volume, implicit):
        result = run_backtest(calendar, schedules, D(10000), [invalid], orders=[request])
        assert not result.fills
        assert [row.code for row in result.audit] == ["missing_open_volume"]
    zero_open = entry.model_copy(update={"open_volume": 0})
    result = run_backtest(calendar, schedules, D(10000), [zero_open], orders=[request])
    assert not result.fills
    assert [row.code for row in result.audit] == ["insufficient_volume"]
    verified = run_backtest(calendar, schedules, D(10000), [entry], orders=[request])
    assert [fill.quantity for fill in verified.fills] == [2]


def test_opening_capacity_cannot_substitute_for_price_availability(calendar, schedules) -> None:
    entry = bar(at(9, 5), 1000)
    data = entry.model_dump()
    data["open_price_available_at"] = entry.bar_end
    with pytest.raises(ValidationError, match="open price must be available"):
        FeatureEvent.model_validate(data)
    for timestamp in (None, entry.bar_end):
        unpriced = entry.model_copy(update={"open_price_available_at": timestamp})
        result = run_backtest(
            calendar, schedules, D(10000), [unpriced], orders=[order("entry", at(9, 5), "buy")]
        )
        assert not result.fills
        assert [row.code for row in result.audit] == ["missing_open_price"]


@pytest.mark.parametrize(
    "field,code",
    [
        ("open_price_available_at", "missing_open_price"),
        ("open_volume_available_at", "missing_open_volume"),
    ],
)
def test_malformed_opening_evidence_fails_closed(calendar, schedules, field, code) -> None:
    entry = bar(at(9, 5), 1000)
    for invalid in ("not a datetime", 1, entry.bar_start.astimezone(UTC)):
        injected = entry.model_copy(update={field: invalid})
        result = run_backtest(
            calendar, schedules, D(10000), [injected], orders=[order("entry", at(9, 5), "buy")]
        )
        assert not result.fills
        assert [(row.at, row.code) for row in result.audit] == [
            (at(9, 5) + timedelta(microseconds=1), code)
        ]


@pytest.mark.parametrize(
    "late_values,issue",
    [
        ({"close": 9999}, "invalid_price"),
        ({"high": None}, "incomplete_bar"),
        ({"low": 1030}, "invalid_price"),
        ({"volume": 0}, "invalid_open_volume"),
        ({"halted": 2}, "unknown_trading_status"),
        ({"halted": 1}, None),
    ],
)
def test_late_completed_data_cannot_cancel_an_observed_open(
    calendar,
    schedules,
    late_values,
    issue,
) -> None:
    shifted = bar(at(9, 10), 1020, delay=2)
    shifted = shifted.model_copy(update={"values": {**shifted.values, **late_values}})
    result = run_backtest(
        calendar,
        schedules,
        D(2700),
        [bar(at(9, 5), 1000), shifted, bar(at(9, 10), 1000, month=DEC)],
        orders=[order("held", at(9, 5), "buy"), order("new", at(9, 10), "buy", month=DEC)],
    )
    assert [fill.signal_id for fill in result.fills] == ["held", "new"]
    row = next(
        row for row in result.snapshots if row.snapshot_at == at(9, 10) + timedelta(microseconds=1)
    )
    assert row.unrealized == 4000
    assert [(entry.at, entry.code) for entry in result.audit] == (
        [(at(9, 17), issue)] if issue else []
    )


def test_late_bad_full_bar_cannot_cancel_a_verified_gap_stop(calendar, schedules) -> None:
    gap = bar(at(9, 10), 980, delay=2)
    gap = gap.model_copy(update={"values": {**gap.values, "close": None}})
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), gap],
        orders=[order("held", at(9, 5), "buy", stop=990)],
    )
    assert [(row.reason, row.reference_price) for row in result.fills] == [
        ("signal", 1000),
        ("stop", 980),
    ]
    assert result.fills[-1].fill_at == at(9, 10) + timedelta(microseconds=1)
    assert [(row.at, row.code) for row in result.audit] == [(at(9, 17), "incomplete_bar")]


def test_late_intrabar_data_cannot_backfill_a_stop(calendar, schedules) -> None:
    tardy = bar(at(9, 10), 1000, low=980, opening_volume=1, delay=2)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), tardy, bar(at(9, 15), 1010)],
        orders=[order("held", at(9, 5), "buy", stop=990)],
    )
    assert [(fill.signal_id, fill.reason) for fill in result.fills] == [("held", "signal")]
    assert [(entry.at, entry.code) for entry in result.audit] == [(at(9, 17), "late_intrabar_bar")]
    late_snapshot = next(row for row in result.snapshots if row.snapshot_at == at(9, 17))
    assert late_snapshot.positions == {"TX:202611": 1}
    assert late_snapshot.unrealized == 2000


def test_late_intrabar_data_audited_even_after_historical_bracket_is_closed(
    calendar, schedules
) -> None:
    tardy = bar(at(9, 5), 1000, low=980, opening_volume=1, delay=7)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 1000), tardy, bar(at(9, 10), 1000)],
        orders=[order("held", at(9, 0), "buy", stop=990), order("close", at(9, 10), "sell")],
    )
    assert [(fill.signal_id, fill.reason) for fill in result.fills] == [
        ("held", "signal"),
        ("close", "signal"),
    ]
    assert [(row.at, row.code) for row in result.audit] == [(at(9, 17), "late_intrabar_bar")]
    assert not result.snapshots[-1].positions


def test_open_capacity_is_shared_without_disabling_intrabar_stop(calendar, schedules) -> None:
    entry = bar(at(9, 5), 1000, low=980, volume=3).model_copy(update={"open_volume": 1})
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [entry],
        orders=[order("a", at(9, 5), "buy", stop=990), order("b", at(9, 5), "buy", month=NOV)],
    )
    assert [(fill.reason, fill.reference_price) for fill in result.fills] == [
        ("signal", D(1000)),
        ("stop", D(990)),
    ]
    assert result.fills[-1].fill_at == at(9, 10) - timedelta(microseconds=1)
    # The same contract's live bracket precludes adding exposure even without the volume limit.
    ordinary = run_backtest(
        calendar,
        schedules,
        D(10000),
        [entry],
        orders=[order(name, at(9, 5), "buy") for name in ("a", "b")],
    )
    assert [fill.signal_id for fill in ordinary.fills] == ["a"]
    assert [row.code for row in ordinary.audit] == ["insufficient_volume"]


def test_opening_stop_and_timed_exit_require_open_volume(calendar, schedules) -> None:
    entry = bar(at(9, 5), 1000)
    gap = without_open_volume(bar(at(9, 10), 980, volume=0))
    timed = without_open_volume(bar(at(13, 40), 1000))
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [entry, gap, timed],
        orders=[order("entry", at(9, 5), "buy", stop=990, exit_at=at(13, 40))],
    )
    assert [fill.reason for fill in result.fills] == ["signal"]
    assert result.snapshots[-1].positions == {"TX:202611": 1}
    missing = [row for row in result.audit if row.code == "missing_open_volume"]
    assert {(row.at, row.detail.split()[0]) for row in missing} == {
        (at(9, 10) + timedelta(microseconds=1), "stop"),
        (at(13, 40) + timedelta(microseconds=1), "time_exit"),
    }
    assert "time_exit_unresolved" in {row.code for row in result.audit}

    intrabar = without_open_volume(bar(at(9, 10), 980, low=970))
    coarse = run_backtest(
        calendar,
        schedules,
        D(10000),
        [entry, intrabar],
        orders=[order("entry", at(9, 5), "buy", stop=990)],
    )
    assert [fill.reason for fill in coarse.fills] == ["signal"]
    assert [
        (row.at, row.code) for row in coarse.audit if row.code == "unknown_intrabar_extrema"
    ] == [(at(9, 15), "unknown_intrabar_extrema")]


@pytest.mark.parametrize("opening,stop,target", [(1030, None, 1020), (980, 990, None)])
@pytest.mark.parametrize(
    "evidence", [{"open_price_available_at": None}, {"open_volume": None}, {"open_volume": 0}]
)
def test_unverified_opening_extreme_cannot_activate_an_intrabar_bracket(
    calendar,
    schedules,
    opening,
    stop,
    target,
    evidence,
) -> None:
    extrema = bar(
        at(9, 10),
        opening,
        low=min(opening, 1000),
        high=max(opening, 1000),
        close=1000,
        volume=2,
        opening_volume=1,
    ).model_copy(update=evidence)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), extrema, bar(at(9, 15), 1010)],
        orders=[order("held", at(9, 5), "buy", stop=stop, target=target)],
    )
    assert [fill.reason for fill in result.fills] == ["signal"]
    assert result.snapshots[-1].positions == {"TX:202611": 1}
    assert [
        (row.at, row.code) for row in result.audit if row.code == "unknown_intrabar_extrema"
    ] == [(at(9, 15), "unknown_intrabar_extrema")]


def test_verified_opening_extreme_alone_does_not_retry_a_target_intrabar(
    calendar,
    schedules,
) -> None:
    gap = bar(at(9, 10), 1030, low=1000, close=1000, volume=3, opening_volume=1)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), gap],
        orders=[order("held", at(9, 5), "buy", quantity=2, target=1020)],
    )
    assert [fill.reason for fill in result.fills] == ["signal"]
    assert result.snapshots[-1].positions == {"TX:202611": 2}
    assert [(row.at, row.code) for row in result.audit] == [
        (at(9, 10) + timedelta(microseconds=1), "insufficient_volume"),
    ]


@pytest.mark.parametrize("opening_volume", [None, 0])
def test_carried_opening_price_does_not_trigger_pending_stop(
    calendar,
    schedules,
    opening_volume,
) -> None:
    carry = bar(at(9, 10), 980, volume=0).model_copy(update={"open_volume": opening_volume})
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), carry, bar(at(9, 15), 1010)],
        orders=[order("entry", at(9, 5), "buy", stop=990)],
    )
    assert [fill.reason for fill in result.fills] == ["signal"]
    assert result.snapshots[-1].positions == {"TX:202611": 1}


def test_intrabar_capacity_excludes_all_observed_opening_volume(calendar, schedules) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(50000),
        [bar(at(9, 5), 1000), bar(at(9, 10), 1000, low=980, volume=10, opening_volume=9)],
        orders=[order("entry", at(9, 5), "buy", quantity=5, stop=990)],
    )
    assert [(fill.reason, fill.quantity) for fill in result.fills] == [("signal", 5), ("stop", 1)]
    assert result.snapshots[-1].positions == {"TX:202611": 4}
    assert [row.code for row in result.audit] == ["stop_partial"]


@pytest.mark.parametrize(
    "opening_volume,code", [(None, "missing_open_volume"), (0, "insufficient_volume")]
)
def test_open_mark_without_opening_capacity_does_not_allow_trades(
    calendar,
    schedules,
    opening_volume,
    code,
) -> None:
    gap = bar(at(9, 10), 1020).model_copy(update={"open_volume": opening_volume})
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), gap],
        orders=[
            order("entry", at(9, 5), "buy"),
            order("new", at(9, 10), "buy"),
            order("exit", at(9, 10), "sell"),
        ],
    )
    assert [fill.signal_id for fill in result.fills] == ["entry"]
    snapshot = next(
        row for row in result.snapshots if row.snapshot_at == at(9, 10) + timedelta(microseconds=1)
    )
    assert snapshot.positions == {"TX:202611": 1}
    assert snapshot.equity - snapshot.cash == 4000
    assert {(row.order_id, row.code) for row in result.audit} == {
        ("new:order", code),
        ("exit:order", code),
    }


def test_later_bar_volume_cannot_change_earlier_marks_or_margin_decisions(
    calendar, schedules
) -> None:
    entry = bar(at(9, 5), 1000)
    other_contract = bar(at(9, 10), 1000, month=DEC)
    decisions = [order("held", at(9, 5), "buy"), order("new", at(9, 10), "buy", month=DEC)]
    for volume in (0, 1):
        gap = bar(at(9, 10), 1020, volume=volume, opening_volume=0, delay=2).model_copy(
            update={"open_price_available_at": None}
        )
        result = run_backtest(
            calendar, schedules, D(2700), [entry, gap, other_contract], orders=decisions
        )
        assert [row.signal_id for row in result.fills] == ["held"]
        assert [(row.order_id, row.code) for row in result.audit if row.at < at(9, 15)] == [
            ("new:order", "initial_margin")
        ]
        assert len([row for row in result.audit if row.code == "late_intrabar_bar"]) == volume
        snapshots = {row.snapshot_at: row for row in result.snapshots}
        assert snapshots[at(9, 10) + timedelta(microseconds=1)].unrealized == 0
        assert snapshots[at(9, 15)].unrealized == 0
        assert snapshots[at(9, 17)].unrealized == 4000 * volume


def test_completed_bar_volume_cannot_trigger_a_stop_at_an_unverified_open(
    calendar, schedules
) -> None:
    entry = bar(at(9, 5), 1000)
    request = order("held", at(9, 5), "buy", stop=990)
    for volume in (0, 1):
        gap = bar(at(9, 10), 980, volume=volume, opening_volume=0).model_copy(
            update={"open_price_available_at": None}
        )
        result = run_backtest(calendar, schedules, D(10000), [entry, gap], orders=[request])
        opening_at = at(9, 10) + timedelta(microseconds=1)
        snapshot = next(row for row in result.snapshots if row.snapshot_at == opening_at)
        assert snapshot.unrealized == 0
        assert [(row.fill_at, row.reason) for row in result.fills if row.fill_at <= opening_at] == [
            (at(9, 5) + timedelta(microseconds=1), "signal")
        ]
        assert not any(row.at == opening_at for row in result.audit)
        assert len(result.fills) == 1
        assert (
            len([row for row in result.audit if row.code == "unknown_intrabar_extrema"]) == volume
        )


def test_delayed_close_does_not_replace_a_newer_observed_open(calendar, schedules) -> None:
    late = bar(at(9, 10), 1020, delay=2).model_copy(update={"open_price_available_at": None})
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), late, bar(at(9, 15), 1040)],
        orders=[order("held", at(9, 5), "buy")],
    )
    snapshots = {row.snapshot_at: row for row in result.snapshots}
    assert snapshots[at(9, 10) + timedelta(microseconds=1)].unrealized == 0
    assert snapshots[at(9, 15) + timedelta(microseconds=1)].unrealized == 8000
    assert snapshots[at(9, 17)].unrealized == 8000


def test_replay_cutoff_excludes_equal_time_prices_orders_and_features(calendar, schedules) -> None:
    visible = []

    def on_feature(feature: FeatureEvent):
        visible.append(feature)
        return ()

    cutoff = at(9, 10)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 5), 1000), bar(cutoff, 1020)],
        before=cutoff,
        orders=[
            order("held", at(9, 5), "buy"),
            order("queued", at(9, 6), "sell"),
            order("late", cutoff, "sell"),
        ],
        on_feature=on_feature,
    )
    assert not visible
    assert [row.signal_id for row in result.orders] == ["held", "queued"]
    assert [row.signal_id for row in result.fills] == ["held"]
    assert [(row.at, row.code) for row in result.audit] == [(at(9, 6), "outside_replay")]
    assert all(row.snapshot_at < cutoff for row in result.snapshots)
    assert result.snapshots[-1].equity == result.snapshots[-1].cash


def test_margin_and_expiry_do_not_use_total_bar_volume_at_open(calendar, schedules) -> None:
    margin = run_backtest(
        calendar,
        schedules,
        D(2500),
        [bar(at(9, 5), 1000, low=990, close=990), without_open_volume(bar(at(9, 10), 990))],
        orders=[order("entry", at(9, 5), "buy")],
    )
    assert [fill.reason for fill in margin.fills] == ["signal"]
    assert any(row.code == "missing_open_volume" and "margin" in row.detail for row in margin.audit)

    expiry = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(13, 20, 18), 1000), without_open_volume(bar(at(13, 25, 18), 1000))],
        orders=[order("entry", at(13, 20, 18), "buy")],
    )
    assert [fill.reason for fill in expiry.fills] == ["signal"]
    assert any(row.code == "missing_open_volume" and "expiry" in row.detail for row in expiry.audit)


def test_timed_exit_is_canceled_after_stop_and_audits_missing_bar(calendar, schedules) -> None:
    request = order("timed", at(9, 5), "buy", stop=990, exit_at=at(13, 40))
    entry = bar(at(9, 5), 1000)
    stop = bar(at(9, 10), 1000, low=980, opening_volume=1)
    closed = run_backtest(calendar, schedules, D(10000), [entry, stop], orders=[request])
    assert [fill.reason for fill in closed.fills] == ["signal", "stop"]
    assert not any(audit.code.startswith("time_exit") for audit in closed.audit)

    later = bar(at(15, 0), 1010, night=True, trade_date=date(2026, 11, 16))
    missing = run_backtest(calendar, schedules, D(10000), [entry, later], orders=[request])
    assert [fill.reason for fill in missing.fills] == ["signal"]
    assert [a.at for a in missing.audit if a.code == "time_exit_unfilled"] == [
        at(13, 40) + timedelta(microseconds=1)
    ]
    assert missing.snapshots[-1].positions == {"TX:202611": 1}


def test_timed_exit_partial_liquidity_and_invalid_session(calendar, schedules) -> None:
    entry = bar(at(9, 5), 1000, volume=2)
    close = bar(at(13, 40), 1020, volume=1)
    request = order("timed", at(9, 5), "buy", quantity=2, exit_at=at(13, 40))
    result = run_backtest(calendar, schedules, D(10000), [entry, close], orders=[request])
    assert [(f.reason, f.quantity) for f in result.fills] == [("signal", 2), ("time_exit", 1)]
    assert [a.code for a in result.audit] == ["time_exit_partial", "time_exit_unresolved"]
    assert result.snapshots[-1].positions == {"TX:202611": 1}
    unavailable = run_backtest(
        calendar, schedules, D(10000), [bar(at(9, 5), 1000, volume=1), close], orders=[request]
    )
    assert not unavailable.fills
    assert [a.code for a in unavailable.audit] == ["insufficient_volume"]

    overnight = order("timed", at(9, 5), "buy", exit_at=at(15, 0))
    invalid = run_backtest(calendar, schedules, D(10000), [entry, close], orders=[overnight])
    assert not invalid.fills
    assert [a.code for a in invalid.audit] == ["invalid_time_exit"]


@pytest.mark.parametrize(
    "side,opening,fill_price,trigger_price,stop_extreme",
    [("buy", 1020, 1021, 1014, {"low": 1013}), ("sell", 980, 979, 986, {"high": 987})],
)
def test_relative_stop_is_rounded_from_the_slipped_entry_fill(
    calendar, schedules, side, opening, fill_price, trigger_price, stop_extreme
) -> None:
    request = order("atr", at(9, 5), side, stop_distance=D("1.5") * D("5.1"))
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(9, 0), 1000),
            bar(at(9, 5), opening),
            bar(at(9, 10), opening, opening_volume=1, **stop_extreme),
        ],
        orders=[request],
    )
    entry, stop = result.fills
    assert (entry.reason, entry.price) == ("signal", fill_price)
    assert (stop.reason, stop.reference_price) == ("stop", trigger_price)
    assert abs(stop.reference_price - entry.price) == 7


def test_relative_target_is_anchored_to_slipped_entry_fill(calendar, schedules) -> None:
    request = order("target-distance", at(9, 5), "buy", target_distance=D(5))
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(9, 0), 1000),
            bar(at(9, 5), 1020),
            bar(at(9, 10), 1022, high=1027, low=1020, close=1025, opening_volume=1),
        ],
        orders=[request],
    )

    entry, target = result.fills
    assert (entry.reason, entry.price) == ("signal", D(1021))
    assert (target.reason, target.reference_price, target.price) == (
        "target",
        D(1026),
        D(1025),
    )


def test_missed_timed_exit_blocks_same_product_across_months_until_flat(
    calendar, schedules
) -> None:
    entry = order("timed", at(9, 5), "buy", exit_at=at(13, 40))
    blocked = order("blocked", at(9, 0, 16), "buy", month=DEC)
    reduce = order("reduce", at(9, 5, 16), "sell")
    allowed = order("allowed", at(9, 10, 16), "buy", month=DEC)
    settle = SettlementEvent(
        instrument_id="TX",
        contract_month=NOV,
        trade_date=DAY,
        available_at=at(14, 0),
        price=1000,
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(9, 5), 1000),
            bar(at(9, 0, 16), 1020, month=DEC),
            bar(at(9, 5, 16), 1000),
            bar(at(9, 10, 16), 1010, month=DEC),
        ],
        orders=[entry, blocked, reduce, allowed],
        settlements=[settle],
    )
    assert [row.signal_id for row in result.fills] == ["timed", "reduce", "allowed"]
    assert [row.event for row in result.settlements] == [settle]
    assert [(row.code, row.order_id, row.contract_month) for row in result.audit] == [
        ("time_exit_unfilled", None, NOV),
        ("time_exit_unresolved", "timed:order", NOV),
        ("overdue_time_exit", "blocked:order", DEC),
    ]


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


@pytest.fixture
def schedules() -> Schedules:
    return Schedules(
        costs={
            "TX": (
                Costs(
                    effective_from=DAY,
                    broker_fee_per_contract=D("13"),
                    tax_rate=D(".00002"),
                    slippage_ticks=1,
                    final_settlement_fee_per_contract=D("8"),
                    final_settlement_tax_rate=D(".00002"),
                ),
            ),
        },
        margins={"TX": (Margin(effective_from=at(0, 0), initial=D(1500), maintenance=D(1000)),)},
    )


def test_golden_long_partial_and_fees(calendar, schedules) -> None:
    bars = [bar(at(9, 0), 1000), bar(at(9, 5), 1000), bar(at(9, 10), 1010), bar(at(9, 15), 1020)]
    requests = [
        order("enter", at(9, 5), "buy", quantity=2),
        order("partial", at(9, 10), "sell"),
        order("exit", at(9, 15), "sell"),
    ]
    result = run_backtest(calendar, schedules, D(10000), bars, orders=requests)
    entry, partial, exit_ = result.fills
    assert entry.fill_at == at(9, 5) + timedelta(microseconds=1)
    assert all(fill.fill_at > fill.signal_at for fill in result.fills)
    assert [(f.price, f.gross_pnl, f.slippage, f.broker_fee, f.tax) for f in result.fills] == [
        (1001, 0, 400, 26, D("8.008")),
        (1009, 2000, 200, 13, D("4.036")),
        (1019, 4000, 200, 13, D("4.076")),
    ]
    partial_book = next(row for row in result.snapshots if row.snapshot_at == partial.fill_at)
    assert partial_book.positions == {"TX:202611": 1}
    assert partial_book.margin_used == 1500
    assert partial_book.unrealized == 2000
    assert partial_book.cash == D("11348.956")
    assert partial_book.equity == D("13348.956")
    final = result.snapshots[-1]
    assert final.cash == final.equity == D("15131.880")
    assert (final.gross_pnl, final.broker_fee, final.tax, final.slippage, final.margin_used) == (
        6000,
        52,
        D("16.120"),
        800,
        0,
    )
    with localcontext() as context:
        context.prec = 4
        replay = run_backtest(
            calendar, schedules, D(10000), reversed(bars), orders=reversed(requests)
        )
    assert replay.model_dump_json().encode() == result.model_dump_json().encode()
    assert entry.reason == partial.reason == exit_.reason == "signal"


def test_golden_short_daily_mtm_then_exit(calendar, schedules) -> None:
    trade_date = date(2026, 11, 16)
    event = SettlementEvent(
        instrument_id="TX", contract_month=NOV, trade_date=DAY, available_at=at(13, 45), price=990
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(13, 40), 1000), bar(at(15, 0), 980, trade_date=trade_date, night=True)],
        orders=[order("short", at(13, 40), "sell"), order("cover", at(13, 45), "buy")],
        settlements=[event],
    )
    assert [fill.price for fill in result.fills] == [999, 981]
    assert result.settlements[0].gross_pnl == 2000
    assert result.fills[-1].gross_pnl == 2000
    daily = next(row for row in result.snapshots if row.snapshot_at == event.available_at)
    assert (daily.cash, daily.equity, daily.unrealized, daily.margin_used) == (
        D("11783.004"),
        D("11783.004"),
        0,
        1500,
    )
    assert result.snapshots[-1].equity == D("13566.080")


def test_golden_atomic_roll_uses_two_contract_prices(calendar, schedules) -> None:
    roll = RollRequest(
        signal=signal("roll", at(9, 5), "buy"),
        from_contract_month=NOV,
        to_contract_month=DEC,
        quantity=1,
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 1000), bar(at(9, 5), 1010), bar(at(9, 5), 1200, month=DEC)],
        orders=[order("entry", at(9, 0), "buy"), roll],
    )
    _, closing, opening = result.fills
    assert closing.fill_at == opening.fill_at
    assert (closing.contract_month, closing.side, closing.price, closing.gross_pnl) == (
        NOV,
        "sell",
        1009,
        2000,
    )
    assert (opening.contract_month, opening.side, opening.price, opening.gross_pnl) == (
        DEC,
        "buy",
        1201,
        0,
    )
    assert result.snapshots[-1].positions == {"TX:202612": 1}
    assert result.snapshots[-1].equity == D("11348.956") - D(".800")
    rejected = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 1000), bar(at(9, 5), 1010), bar(at(9, 5), 1200, month=DEC, volume=0)],
        orders=[order("entry", at(9, 0), "buy"), roll],
    )
    assert len(rejected.fills) == 1
    assert rejected.snapshots[-1].positions == {"TX:202611": 1}
    assert sum(issue.code == "insufficient_volume" for issue in rejected.audit) == 2


@pytest.mark.parametrize(
    ("opening", "expected_ref", "equity"),
    [
        (980, 980, "5566.080"),
        (1000, 990, "7566.040"),
    ],
)
def test_golden_stop_gap_and_same_bar_oco(
    calendar, schedules, opening, expected_ref, equity
) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 1000), bar(at(9, 5), opening, low=970, high=1030, opening_volume=1)],
        orders=[order("entry", at(9, 0), "buy", stop=990, target=1020)],
    )
    assert [(fill.reason, fill.reference_price, fill.price) for fill in result.fills] == [
        ("signal", 1000, 1001),
        ("stop", expected_ref, expected_ref - 1),
    ]
    assert result.snapshots[-1].equity == D(equity)


def test_golden_margin_refusal_and_forced_exit(calendar, schedules) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(2500),
        [
            bar(at(9, 0), 1000),
            bar(at(9, 5), 1000, close=990, low=990),
            bar(at(9, 10), 990, volume=0),
            bar(at(9, 15), 989),
        ],
        orders=[
            order("too_large", at(9, 0), "buy", quantity=2),
            order("accepted", at(9, 5), "buy"),
            order("blocked", at(9, 10), "buy"),
        ],
    )
    assert [row.reason for row in result.fills] == ["signal", "margin"]
    assert result.fills[-1].fill_at == at(9, 15) + timedelta(microseconds=1)
    assert result.fills[-1].gross_pnl == -2200
    assert result.snapshots[-1].equity == D("-133.956")
    assert result.snapshots[-1].margin_used == 0
    assert {row.code for row in result.audit} >= {
        "initial_margin",
        "margin_call",
        "margin_unfilled",
    }


def test_late_feature_missing_open_wrong_session_and_missing_schedules(calendar, schedules) -> None:
    shown = []
    invalid = bar(at(9, 20), 1000, trade_date=date(2026, 11, 12))
    delayed = bar(at(9, 0), 1000, delay=2)

    def decide(feature):
        shown.append(feature)
        return [order("callback", feature.available_at, "buy")]

    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [delayed, invalid, bar(at(9, 15), 1000)],
        on_feature=decide,
    )
    assert shown == [delayed, bar(at(9, 15), 1000)]
    assert result.fills == ()
    assert {issue.code for issue in result.audit} >= {"closed_or_unlisted", "missing_open"}
    for missing, code in [
        (Schedules(costs={}, margins=schedules.margins), "missing_cost"),
        (Schedules(costs=schedules.costs, margins={}), "missing_margin"),
    ]:
        result = run_backtest(
            calendar,
            missing,
            D(10000),
            [bar(at(9, 0), 1000)],
            orders=[order("one", at(9, 0), "buy")],
        )
        assert result.fills == ()
        assert [issue.code for issue in result.audit] == [code]


def test_expiry_exit_cash_settlement_and_unlisted_months(calendar, schedules) -> None:
    final_at = at(13, 30, 18)
    event = SettlementEvent(
        instrument_id="TX",
        contract_month=NOV,
        trade_date=final_at.date(),
        available_at=final_at,
        price=1010,
        final=True,
    )
    history = [
        bar(at(13, 20, 18), 1000),
        bar(at(13, 25, 18), 1010, volume=0),
        bar(at(13, 30, 18), 1020),
        bar(at(13, 35, 18), 1030, month="202705"),
    ]
    requests = [
        order("entry", at(13, 20, 18), "buy"),
        order("expired", final_at, "buy"),
        order("unlisted", final_at, "buy", month="202705"),
    ]
    result = run_backtest(
        calendar, schedules, D(10000), history, orders=requests, settlements=[event]
    )
    assert len(result.fills) == 1
    assert result.settlements[0].gross_pnl == 2000
    assert result.settlements[0].tax == D("4.040")
    assert result.snapshots[-1].equity == D("11770.956")
    assert result.snapshots[-1].positions == {}
    assert {issue.code for issue in result.audit} == {
        "expiry_unfilled",
        "closed_or_unlisted",
        "calendar_gap",
    }
    assert next(issue for issue in result.audit if issue.order_id == "expired:order").code == (
        "closed_or_unlisted"
    )
    assert next(issue for issue in result.audit if issue.order_id == "unlisted:order").code == (
        "calendar_gap"
    )
    without_final = run_backtest(calendar, schedules, D(10000), history, orders=requests)
    assert without_final.settlements == ()
    assert without_final.snapshots[-1].positions == {"TX:202611": 1}
    assert "expiry_unsettled" in {issue.code for issue in without_final.audit}
    with_open = run_backtest(
        calendar,
        schedules,
        D(10000),
        [history[0], bar(at(13, 25, 18), 1010)],
        orders=requests[:1],
    )
    assert [fill.reason for fill in with_open.fills] == ["signal", "expiry"]


@pytest.mark.parametrize(
    ("side", "stop", "target"),
    [
        ("buy", 990, None),
        ("buy", None, 975),
        ("buy", None, 981),
        ("sell", 970, None),
        ("sell", None, 985),
        ("sell", None, 979),
    ],
)
def test_entry_fill_outside_bracket_is_rejected(
    calendar,
    schedules,
    side,
    stop,
    target,
) -> None:
    rejected = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 980, low=970, high=1005)],
        orders=[order("entry", at(9, 0), side, stop=stop, target=target)],
    )
    assert rejected.fills == ()
    assert [issue.code for issue in rejected.audit] == ["invalid_bracket"]


def test_margin_update_at_day_close_and_no_reentry_at_liquidation_open(calendar, schedules) -> None:
    schedules = Schedules(
        costs=schedules.costs,
        margins={
            "TX": (
                *schedules.margins["TX"],
                Margin(effective_from=at(13, 45), initial=D(16000), maintenance=D(15000)),
            )
        },
    )
    event = SettlementEvent(
        instrument_id="TX", contract_month=NOV, trade_date=DAY, available_at=at(13, 45), price=1000
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(13, 40), 1000), bar(at(15, 0), 1000, trade_date=date(2026, 11, 16), night=True)],
        orders=[order("entry", at(13, 40), "buy"), order("reentry", at(15, 0), "buy")],
        settlements=[event],
    )
    assert [fill.reason for fill in result.fills] == ["signal", "margin"]
    daily = next(row for row in result.snapshots if row.snapshot_at == at(13, 45))
    assert daily.equity == D("9782.996")
    assert daily.margin_used == 16000
    assert daily.maintenance_margin == 15000
    assert result.snapshots[-1].equity == D("9566.000")
    assert [issue.code for issue in result.audit if issue.order_id == "reentry:order"] == [
        "margin_call"
    ]


def test_repeated_settlements_are_one_audit_and_never_mark_to_market(calendar, schedules) -> None:
    event = SettlementEvent(
        instrument_id="TX", contract_month=NOV, trade_date=DAY, available_at=at(13, 45), price=1010
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(13, 40), 1000), bar(at(15, 0), 1000, trade_date=date(2026, 11, 16), night=True)],
        orders=[order("entry", at(13, 40), "buy")],
        settlements=[event, event],
    )
    assert result.settlements == ()
    assert [(issue.code, issue.at) for issue in result.audit] == [
        ("duplicate_settlement", at(13, 45)),
        ("missing_settlement", at(15, 0)),
    ]
    assert result.snapshots[-1].cash == D("9782.996")


def test_late_revision_of_execution_bar_aborts_run_before_callbacks(calendar, schedules) -> None:
    original = bar(at(9, 0), 1000)
    revised = bar(at(9, 0), 1005, delay=15)
    request = order("entry", at(9, 0), "buy")
    seen = []

    def on_feature(feature):
        seen.append(feature)
        return ()

    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [original],
        orders=[request],
        on_feature=on_feature,
    )
    assert [fill.price for fill in result.fills] == [1001]
    assert seen == [original]
    seen.clear()
    with pytest.raises(ValueError, match="duplicate execution bar for TX:202611 at"):
        run_backtest(
            calendar,
            schedules,
            D(10000),
            [original, revised],
            orders=[request],
            on_feature=on_feature,
        )
    assert seen == []


def test_missing_daily_settlement_blocks_new_exposure_until_position_is_flat(
    calendar, schedules
) -> None:
    night = date(2026, 11, 16)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(13, 40), 1000),
            bar(at(15, 0), 1000, trade_date=night, night=True),
            bar(at(15, 5), 1000, trade_date=night, night=True),
        ],
        orders=[
            order("entry", at(13, 40), "buy"),
            order("blocked", at(15, 0), "buy"),
            order("close", at(15, 0), "sell"),
            order("reenter", at(15, 5), "buy"),
        ],
    )
    assert [fill.signal_id for fill in result.fills] == ["entry", "close", "reenter"]
    assert result.snapshots[-1].positions == {"TX:202611": 1}
    assert [(row.code, row.order_id, row.at) for row in result.audit] == [
        ("missing_settlement", None, at(15, 0)),
        ("unsettled_account", "blocked:order", at(15, 0) + timedelta(microseconds=1)),
    ]


def test_future_month_first_trades_at_listing_day_open(calendar, schedules) -> None:
    first_open = at(8, 45, 19)
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(first_open, 1000, month="202702")],
        orders=[order("first_listing", at(14, 0, 18), "buy", month="202702")],
    )
    (fill,) = result.fills
    assert fill.contract_month == "202702"
    assert fill.fill_at == first_open + timedelta(microseconds=1)
    assert result.audit == ()


def test_feature_callbacks_receive_independent_market_data_copies(calendar, schedules) -> None:
    entry = bar(at(9, 0), 1000)
    observed: list[int | Decimal | None] = []

    def mutate(feature: FeatureEvent):
        feature.values["close"] = 2000
        return ()

    def observe(feature: FeatureEvent, positions: Mapping[tuple[str, str], int]):
        observed.append(feature.values["close"])
        return ()

    run_backtest(
        calendar,
        schedules,
        D(10000),
        [entry],
        on_feature=mutate,
        on_feature_with_positions=observe,
    )

    assert observed == [1000]


def test_order_exposure_limit_is_checked_across_contract_months(calendar, schedules) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 1000, month=NOV), bar(at(9, 0), 1200, month=DEC)],
        orders=[
            order("a", at(9, 0), "buy", month=NOV, max_instrument_exposure=1),
            order("b", at(9, 0), "buy", month=DEC, max_instrument_exposure=1),
        ],
    )

    assert len(result.fills) == 1
    assert result.snapshots[-1].positions == {"TX:202611": 1}
    assert [row.code for row in result.audit] == ["position_limit"]


def test_reduce_only_order_is_not_rejected_by_new_exposure_limit(calendar, schedules) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(9, 0), 1000, month=NOV),
            bar(at(9, 0), 1200, month=DEC),
            bar(at(9, 5), 1005, month=NOV),
        ],
        orders=[
            order("a", at(9, 0), "buy", quantity=2, month=NOV),
            order("b", at(9, 0), "buy", month=DEC),
            order(
                "reduce",
                at(9, 5),
                "sell",
                month=NOV,
                reduce_only=True,
                max_instrument_exposure=1,
            ),
        ],
    )

    assert [fill.signal_id for fill in result.fills] == ["a", "b", "reduce"]
    assert result.snapshots[-1].positions == {"TX:202611": 1, "TX:202612": 1}
    assert not any(row.code == "position_limit" for row in result.audit)


def test_delayed_decision_after_cutoff_does_not_require_a_replayed_source_bar(
    calendar, schedules
) -> None:
    source = bar(at(10, 0), 1000)
    decision = source.model_copy(update={"available_at": at(10, 8)})

    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [source],
        before=at(9, 30),
        decision_features=[decision],
        on_feature_with_positions=lambda feature, positions: [
            order("late", feature.available_at, "buy")
        ],
    )

    assert result.fills == ()


def test_invalid_market_bar_cannot_trigger_a_delayed_decision(calendar, schedules) -> None:
    source = bar(at(9, 0), 1000).model_copy(
        update={"values": {**bar(at(9, 0), 1000).values, "close": None}}
    )
    decision = source.model_copy(update={"available_at": at(9, 13)})
    seen: list[FeatureEvent] = []

    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [source],
        decision_features=[decision],
        on_feature_with_positions=lambda feature, positions: seen.append(feature) or (),
    )

    assert seen == []
    assert [row.code for row in result.audit] == ["incomplete_bar"]


@pytest.mark.parametrize("volume", [1, 2])
def test_entry_open_equal_to_stop_rejects_even_when_slipped_price_is_inside(
    calendar, schedules, volume
) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(9, 0), 1000, low=1000, high=1010, close=1010, volume=volume)],
        orders=[order("entry", at(9, 0), "buy", stop=1000)],
    )
    assert result.fills == ()
    assert [issue.code for issue in result.audit] == ["invalid_bracket"]


@pytest.mark.parametrize("hour", [14, 15])
def test_daily_settlement_accepted_through_next_open(calendar, schedules, hour) -> None:
    event = SettlementEvent(
        instrument_id="TX",
        contract_month=NOV,
        trade_date=DAY,
        available_at=at(hour, 0),
        price=1010,
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(13, 40), 1000), bar(at(15, 0), 1010, trade_date=date(2026, 11, 16), night=True)],
        orders=[order("entry", at(13, 40), "buy"), order("add", at(15, 0), "buy")],
        settlements=[event],
    )
    (record,) = result.settlements
    assert record.event == event
    assert record.gross_pnl == 2000
    assert [fill.signal_id for fill in result.fills] == ["entry", "add"]
    assert result.audit == ()


def test_order_extends_horizon_to_expiry_close_without_a_final_bar(calendar, schedules) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [bar(at(13, 20, 18), 1000)],
        orders=[
            order("entry", at(13, 20, 18), "buy"),
            order("next_month", at(13, 29, 18), "buy", month=DEC),
        ],
    )
    assert [fill.signal_id for fill in result.fills] == ["entry"]
    assert [(row.code, row.at, row.contract_month) for row in result.audit] == [
        ("expiry_unfilled", at(13, 25, 18) + timedelta(microseconds=1), NOV),
        ("expiry_unsettled", at(13, 30, 18), NOV),
        ("missing_open", at(13, 30, 18) + timedelta(microseconds=1), DEC),
    ]


@pytest.mark.parametrize("has_final", [True, False])
def test_expiry_pending_blocks_until_official_final_settlement(
    calendar, schedules, has_final
) -> None:
    event = SettlementEvent(
        instrument_id="TX",
        contract_month=NOV,
        trade_date=date(2026, 11, 18),
        available_at=at(14, 0, 18),
        price=1010,
        final=True,
    )
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(13, 20, 18), 1000),
            bar(at(13, 35, 18), 1000, month=DEC),
            bar(at(15, 0, 18), 1000, month=DEC, trade_date=date(2026, 11, 19), night=True),
        ],
        orders=[
            order("entry", at(13, 20, 18), "buy"),
            order("before_final", at(13, 35, 18), "buy", month=DEC),
            order("after_final", at(15, 0, 18), "buy", month=DEC),
        ],
        settlements=[event] if has_final else [],
    )
    expiry_audits = [row for row in result.audit if row.code == "expiry_unsettled"]
    blocked = [row.order_id for row in result.audit if row.code == "unsettled_account"]
    if has_final:
        (settlement,) = result.settlements
        assert settlement.event == event
        assert settlement.gross_pnl == 2000
        assert expiry_audits == []
        assert [fill.signal_id for fill in result.fills] == ["entry", "after_final"]
        assert blocked == ["before_final:order"]
    else:
        assert result.settlements == ()
        assert [row.at for row in expiry_audits] == [at(15, 5, 18)]
        assert result.snapshots[-1].positions == {"TX:202611": 1}
        assert blocked == ["before_final:order", "after_final:order"]


def test_partial_stop_retries_at_next_open_even_after_price_recovers(calendar, schedules) -> None:
    result = run_backtest(
        calendar,
        schedules,
        D(10000),
        [
            bar(at(9, 0), 1000, volume=2),
            bar(at(9, 5), 995, low=989, volume=2, opening_volume=1),
            bar(at(9, 10), 1001, volume=1),
        ],
        orders=[order("entry", at(9, 0), "buy", quantity=2, stop=990)],
    )
    assert [(fill.reason, fill.quantity, fill.reference_price) for fill in result.fills] == [
        ("signal", 2, 1000),
        ("stop", 1, 990),
        ("stop", 1, 1001),
    ]
    assert result.fills[-1].fill_at == at(9, 10) + timedelta(microseconds=1)
    assert [issue.code for issue in result.audit] == ["stop_partial"]
    assert result.snapshots[-1].positions == {}
