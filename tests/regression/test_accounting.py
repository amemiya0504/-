from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

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
        values={
            "open": price,
            "high": high or price,
            "low": low or price,
            "close": close or price,
            "volume": volume,
        },
    )


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
    target: int | None = None,
) -> OrderRequest:
    return OrderRequest(
        signal=signal(tag, decision, side),
        contract_month=month,
        quantity=quantity,
        stop_price=stop,
        target_price=target,
    )


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
        [bar(at(9, 0), 1000), bar(at(9, 5), opening, low=970, high=1030)],
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
            bar(at(9, 5), 995, low=989, volume=1),
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
