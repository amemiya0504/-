from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.features.basis import (
    BASIS_LOOKBACK,
    BasisPoint,
    CarryInputs,
    basis_zscore,
    make_basis_point,
    past_zscore,
)
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.strategies.basis_reversion import compare_basis

D = Decimal
MONTH = "202612"
TRADE_DATE = date(2026, 11, 13)


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


def at(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TAIPEI)


def future_bar(
    start: datetime,
    close: int = 27500,
    *,
    month: str = MONTH,
    session: str = "day",
) -> FeatureEvent:
    return FeatureEvent(
        instrument_id="MTX",
        contract_month=month,
        trade_date=start.date(),
        session=session,
        bar_start=start,
        bar_end=start + timedelta(minutes=5),
        available_at=start + timedelta(minutes=5),
        open_price_available_at=start,
        open_volume=10,
        open_volume_available_at=start,
        values={"open": close, "high": close + 2, "low": close - 2, "close": close, "volume": 10},
    )


def spot_bar(start: datetime, close: int = 27500, *, session: str = "day") -> FeatureEvent:
    return FeatureEvent(
        instrument_id="TAIEX",
        trade_date=start.date(),
        session=session,
        bar_start=start,
        bar_end=start + timedelta(minutes=5),
        available_at=start + timedelta(minutes=5),
        values={"close": close},
    )


def carry(**changes: object) -> CarryInputs:
    return CarryInputs(dividend_status="none", **changes)  # type: ignore[arg-type]


def test_fair_value_basis_uses_only_available_rate_and_dividend(calendar) -> None:
    start = at(13, 9, 0)
    point = make_basis_point(
        spot_bar(start),
        future_bar(start, 27510),
        CarryInputs(
            annual_rate=D("0.05"),
            rate_available_at=at(13, 8, 0),
            dividend_status="modeled",
            dividend_pv=D(20),
            dividend_available_at=at(13, 8, 0),
        ),
        calendar,
    )
    assert point is not None
    assert point.basis_kind == "fair_value"
    assert point.reference_price > D(27480)
    assert point.tau_years > 0
    assert point.residual == point.future_price - point.reference_price


def test_late_rate_is_not_used_and_unknown_dividend_risk_is_rejected(calendar) -> None:
    start = at(13, 9, 0)
    point = make_basis_point(
        spot_bar(start),
        future_bar(start, 27510),
        carry(annual_rate=D("0.1"), rate_available_at=at(13, 9, 6)),
        calendar,
    )
    assert point is not None
    assert point.basis_kind == "simplified"
    assert point.reference_price == D(27500)
    assert point.residual == D(10)

    unknown = CarryInputs(dividend_status="unknown")
    assert make_basis_point(spot_bar(start), future_bar(start), unknown, calendar) is None
    late_dividend = CarryInputs(
        dividend_status="modeled",
        dividend_pv=D(20),
        dividend_available_at=at(13, 9, 6),
    )
    assert make_basis_point(spot_bar(start), future_bar(start), late_dividend, calendar) is None


def test_basis_requires_same_day_bar_and_excludes_expiry_and_night(calendar) -> None:
    start = at(13, 9, 0)
    simple = carry()
    assert make_basis_point(spot_bar(start), future_bar(start), simple, calendar) is not None
    for invalid_start in (
        at(13, 8, 45),
        at(13, 13, 35),
        at(13, 13, 45),
        datetime(2026, 11, 13, 9, 1, tzinfo=TAIPEI),
    ):
        assert (
            make_basis_point(spot_bar(invalid_start), future_bar(invalid_start), simple, calendar)
            is None
        )
    no_volume = future_bar(start).model_copy(
        update={"values": {**future_bar(start).values, "volume": 0}}
    )
    fractional_volume = future_bar(start).model_copy(
        update={"values": {**future_bar(start).values, "volume": D("0.5")}}
    )
    invalid_close = future_bar(start).model_copy(
        update={"values": {**future_bar(start).values, "close": 100000}}
    )
    halted = future_bar(start).model_copy(
        update={"values": {**future_bar(start).values, "halted": 1}}
    )
    assert make_basis_point(spot_bar(start), no_volume, simple, calendar) is None
    assert make_basis_point(spot_bar(start), fractional_volume, simple, calendar) is None
    assert make_basis_point(spot_bar(start), invalid_close, simple, calendar) is None
    assert make_basis_point(spot_bar(start), halted, simple, calendar) is None
    assert (
        make_basis_point(
            spot_bar(start + timedelta(minutes=5)), future_bar(start), simple, calendar
        )
        is None
    )
    assert (
        make_basis_point(spot_bar(start, session="night"), future_bar(start), simple, calendar)
        is None
    )

    expiry_start = datetime(2026, 11, 18, 13, 20, tzinfo=TAIPEI)
    expiry_future = future_bar(expiry_start, month="202611")
    expiry_spot = spot_bar(expiry_start)
    assert make_basis_point(expiry_spot, expiry_future, simple, calendar) is None


def test_zscore_uses_exactly_past_available_points() -> None:
    current_start = at(13, 9, 0)
    current_future = future_bar(current_start, 27600)
    current = BasisPoint(
        spot=spot_bar(current_start),
        future=current_future,
        available_at=current_future.available_at,
        spot_price=D(27500),
        future_price=D(27600),
        reference_price=D(27500),
        residual=D(100),
        tau_years=D("0.01"),
        basis_kind="simplified",
    )
    history = []
    for index in range(BASIS_LOOKBACK):
        start = current_start - timedelta(minutes=5 * (BASIS_LOOKBACK - index))
        future = future_bar(start, 27500 + index)
        spot = spot_bar(start)
        history.append(
            BasisPoint(
                spot=spot,
                future=future,
                available_at=future.available_at,
                spot_price=D(27500),
                future_price=D(27500 + index),
                reference_price=D(27500),
                residual=D(index),
                tau_years=D("0.01"),
                basis_kind="simplified",
            )
        )
    z = basis_zscore(current, history)
    assert z is not None and z > 0
    assert basis_zscore(current, list(reversed(history))) == z
    assert basis_zscore(current, history + [history[0]]) is None
    assert past_zscore(D(1), [D(index) for index in range(BASIS_LOOKBACK - 1)]) is None

    future_start = current_start + timedelta(minutes=5)
    future = future_bar(future_start)
    later = history[0].model_copy(
        update={
            "future": future,
            "available_at": future.available_at,
            "residual": D(100000),
            "future_price": D(127500),
        }
    )
    assert basis_zscore(current, history + [later]) == z


def test_carry_schema_requires_asof_timestamps() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        CarryInputs(annual_rate=D("0.02"), dividend_status="none")
    with pytest.raises(ValueError, match="modeled dividends require"):
        CarryInputs(dividend_status="modeled")


def test_basis_backtest_uses_reduce_only_mean_exit_and_price_baseline(calendar) -> None:
    futures = []
    spots = []
    carry_by_bar = {}
    starts = [
        datetime(2026, 11, 13, 9, 0, tzinfo=TAIPEI) + timedelta(minutes=5 * i) for i in range(54)
    ] + [datetime(2026, 11, 16, 9, 0, tzinfo=TAIPEI) + timedelta(minutes=5 * i) for i in range(9)]
    residuals = list(range(60)) + [100, 30, 30]
    for start, residual in zip(starts, residuals, strict=True):
        futures.append(future_bar(start, 10000 + residual))
        futures.append(future_bar(start, 10000 + residual, month="202701"))
        spots.append(spot_bar(start, 10000))
        carry_by_bar[(MONTH, start)] = carry()
        carry_by_bar[("202701", start)] = carry()
    futures.extend(
        future_bar(start, 10000, month=month)
        for month in (MONTH, "202701")
        for start in (
            [
                datetime(2026, 11, 13, 13, 30, tzinfo=TAIPEI) + timedelta(minutes=5 * index)
                for index in range(3)
            ]
            + [
                datetime(2026, 11, 16, 8, 45, tzinfo=TAIPEI) + timedelta(minutes=5 * index)
                for index in range(3)
            ]
        )
    )
    futures.append(future_bar(datetime(2026, 11, 16, 13, 20, tzinfo=TAIPEI), 10000, month="202611"))
    invalid_near_month = future_bar(
        datetime(2026, 11, 16, 9, 10, tzinfo=TAIPEI), 10000, month="202611"
    )
    futures.append(
        invalid_near_month.model_copy(
            update={
                "values": {**invalid_near_month.values, "close": 100000},
            }
        )
    )
    halted_near_month = future_bar(
        datetime(2026, 11, 16, 9, 15, tzinfo=TAIPEI), 10000, month="202611"
    )
    futures.append(
        halted_near_month.model_copy(
            update={
                "open_volume": 0,
                "values": {**halted_near_month.values, "volume": 0, "halted": 1},
            }
        )
    )
    near_month_at_exit = datetime(2026, 11, 16, 9, 35, tzinfo=TAIPEI)
    futures.append(future_bar(near_month_at_exit, 10000, month="202611"))
    carry_by_bar[("202611", near_month_at_exit)] = carry()
    schedules = Schedules(
        costs={
            "MTX": (
                Costs(
                    effective_from=date(2026, 1, 1),
                    broker_fee_per_contract=D(2),
                    tax_rate=D("0.00002"),
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

    result = compare_basis(spots, futures, carry_by_bar, calendar, schedules, D(1000000))

    assert [fill.reason for fill in result.basis.fills] == ["signal", "signal"]
    assert {fill.contract_month for fill in result.basis.fills} == {MONTH}
    assert result.basis.fills[0].side == "sell"
    assert result.basis.fills[1].side == "buy"
    assert result.basis.fills[1].signal_id.endswith("mean-exit")
    assert result.basis.snapshots[-1].positions == {}
    assert [fill.side for fill in result.price_only.fills] == ["sell", "buy"]
    assert {fill.contract_month for fill in result.price_only.fills} == {MONTH}


def test_compare_basis_ignores_duplicate_bars_after_cutoff(calendar) -> None:
    start = datetime(2026, 11, 16, 10, 0, tzinfo=TAIPEI)
    spot = spot_bar(start)
    future = future_bar(start)
    result = compare_basis(
        [spot, spot],
        [future, future],
        {},
        calendar,
        Schedules(costs={}, margins={}),
        D(100000),
        before=datetime(2026, 11, 16, 9, 30, tzinfo=TAIPEI),
    )

    assert result.basis.fills == result.price_only.fills == ()


def test_compare_basis_ignores_spot_revision_unavailable_at_cutoff(calendar) -> None:
    start = datetime(2026, 11, 16, 9, 0, tzinfo=TAIPEI)
    spot = spot_bar(start)
    late_spot = spot.model_copy(
        update={"available_at": datetime(2026, 11, 16, 14, 0, tzinfo=TAIPEI)}
    )
    future = future_bar(start)

    result = compare_basis(
        [spot, late_spot],
        [future],
        {(MONTH, start): carry()},
        calendar,
        Schedules(costs={}, margins={}),
        D(100000),
        before=datetime(2026, 11, 16, 9, 30, tzinfo=TAIPEI),
    )

    assert result.basis.fills == result.price_only.fills == ()


def test_late_spot_decision_preserves_timely_futures_stop(calendar) -> None:
    futures = []
    spots = []
    carry_by_bar = {}
    starts = (
        [
            datetime(2026, 11, 13, 9, 0, tzinfo=TAIPEI) + timedelta(minutes=5 * index)
            for index in range(54)
        ]
        + [
            datetime(2026, 11, 16, 9, 0, tzinfo=TAIPEI) + timedelta(minutes=5 * index)
            for index in range(6)
        ]
        + [
            datetime(2026, 11, 16, 9, 30, tzinfo=TAIPEI),
            datetime(2026, 11, 16, 9, 35, tzinfo=TAIPEI),
            datetime(2026, 11, 16, 9, 40, tzinfo=TAIPEI),
        ]
    )
    residuals = list(range(60)) + [100, 0, 0]
    for start, residual in zip(starts, residuals, strict=True):
        future = future_bar(start, 10000 + residual)
        if start == starts[-1]:
            future = future.model_copy(
                update={
                    "open_volume": 1,
                    "values": {
                        **future.values,
                        "high": 10150,
                        "low": 9900,
                    },
                }
            )
        spot = spot_bar(start, 10000)
        if start == starts[-3]:
            spot = spot.model_copy(update={"available_at": start + timedelta(minutes=8)})
        futures.append(future)
        spots.append(spot)
        carry_by_bar[(MONTH, start)] = carry()
    futures.extend(
        future_bar(start, 10000)
        for start in (
            [
                datetime(2026, 11, 13, 13, 30, tzinfo=TAIPEI) + timedelta(minutes=5 * index)
                for index in range(3)
            ]
            + [
                datetime(2026, 11, 16, 8, 45, tzinfo=TAIPEI) + timedelta(minutes=5 * index)
                for index in range(3)
            ]
        )
    )
    schedules = Schedules(
        costs={
            "MTX": (
                Costs(
                    effective_from=date(2026, 1, 1),
                    broker_fee_per_contract=D(2),
                    tax_rate=D("0.00002"),
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

    result = compare_basis(spots, futures, carry_by_bar, calendar, schedules, D(1000000)).basis

    assert [fill.reason for fill in result.fills] == ["signal", "stop"]
    assert result.fills[0].signal_at == starts[-3] + timedelta(minutes=8)
    stop_bar = next(row for row in futures if row.bar_start == starts[-1])
    assert result.fills[1].fill_at < stop_bar.available_at
    assert result.snapshots[-1].positions == {}
    cutoff = compare_basis(
        spots,
        futures,
        carry_by_bar,
        calendar,
        schedules,
        D(100000),
        before=starts[-3] + timedelta(minutes=8),
    ).basis
    assert cutoff.fills == ()
