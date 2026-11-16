import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from random import Random
from typing import Literal, Self

import pytest

from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.engine.events import (
    BacktestFill,
    BacktestResult,
    BacktestSnapshot,
    SettlementEvent,
    SettlementRecord,
)
from gtts.research.bootstrap import paired_block_bootstrap
from gtts.research.comparison import (
    build_run_manifest,
    compare_paired_daily_returns,
    cost_stress_scenarios,
    delayed_decision_features,
    only_delayed_position_decisions,
    run_stress_scenarios,
)
from gtts.research.metrics import (
    CompletedTrade,
    calculate_metrics,
    count_completed_position_trades,
)
from gtts.research.splits import fit_standardizer, walk_forward_splits
from gtts.risk.margin import Costs, Schedules

_report_spec = importlib.util.spec_from_file_location(
    "gtts_generate_report", Path(__file__).resolve().parents[2] / "scripts" / "generate_report.py"
)
if _report_spec is None or _report_spec.loader is None:
    raise RuntimeError("cannot load generate_report.py")
generate_report = importlib.util.module_from_spec(_report_spec)
_report_spec.loader.exec_module(generate_report)


def _empty_result(
    *, fills: tuple[BacktestFill, ...] = (), settlements: tuple[SettlementRecord, ...] = ()
) -> BacktestResult:
    return BacktestResult(orders=(), fills=fills, snapshots=(), settlements=settlements, audit=())


def _position_fill(
    fill_id: str,
    trade_date: date,
    offset_seconds: int,
    side: Literal["buy", "sell"],
    *,
    gross_pnl: Decimal = Decimal(0),
    quantity: int = 1,
    contract_month: str = "202612",
    reason: Literal["signal", "roll", "stop", "target", "time_exit", "margin", "expiry"] = "signal",
) -> BacktestFill:
    signal_at = datetime(
        trade_date.year, trade_date.month, trade_date.day, 9, tzinfo=TAIPEI
    )
    return BacktestFill(
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        signal_id=f"signal-{fill_id}",
        instrument_id="MTX",
        side=side,
        quantity=quantity,
        price=Decimal(100),
        signal_at=signal_at,
        fill_at=signal_at + timedelta(seconds=offset_seconds),
        contract_month=contract_month,
        trade_date=trade_date,
        reason=reason,
        multiplier=50,
        reference_price=Decimal(100),
        gross_pnl=gross_pnl,
        broker_fee=Decimal(0),
        tax=Decimal(0),
        slippage=Decimal(0),
    )


def test_walk_forward_keeps_gaps_and_oos_windows_disjoint() -> None:
    sessions = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(100))
    splits = walk_forward_splits(
        sessions,
        train_size=10,
        validation_size=5,
        oos_size=5,
        purge_size=2,
        embargo_size=3,
    )

    assert len(splits) == 3
    for split in splits:
        assert len(split.train) == 10
        assert len(split.validation) == len(split.oos) == 5
        assert set(split.train).isdisjoint(split.validation)
        assert set(split.train).isdisjoint(split.oos)
        assert set(split.validation).isdisjoint(split.oos)
        assert max(split.train) < min(split.validation) < min(split.oos)
        assert len(split.purged) == 4
    assert not set(splits[0].oos) & set(splits[1].oos)
    assert splits[0].embargoed[-1] < splits[1].train[0]


def test_walk_forward_rejects_duplicate_or_unsorted_sessions() -> None:
    sessions = (date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(ValueError, match="chronological"):
        walk_forward_splits(sessions, train_size=1, validation_size=1, oos_size=1)


def test_standardizer_fits_training_rows_and_reuses_scale() -> None:
    fitted = fit_standardizer(((0.0, 5.0), (2.0, 5.0)))

    assert fitted.mean == (1.0, 5.0)
    assert fitted.scale == (1.0, 1.0)
    assert fitted.transform(((101.0, 5.0),)) == ((100.0, 0.0),)
    with pytest.raises(ValueError, match="width"):
        fitted.transform(((1.0,),))


def test_metrics_reconcile_fill_and_settlement_costs_and_completed_trades() -> None:
    signal_at = datetime(2026, 11, 16, 9, tzinfo=TAIPEI)
    fill = BacktestFill(
        fill_id="f1",
        order_id="o1",
        signal_id="s1",
        instrument_id="MTX",
        side="buy",
        quantity=1,
        price=Decimal(100),
        signal_at=signal_at,
        fill_at=signal_at + timedelta(minutes=1),
        contract_month="202612",
        trade_date=date(2026, 11, 16),
        reason="signal",
        multiplier=50,
        reference_price=Decimal(100),
        gross_pnl=Decimal(-20),
        broker_fee=Decimal(5),
        tax=Decimal(2),
        slippage=Decimal(3),
    )
    settlement = SettlementRecord(
        event=SettlementEvent(
            instrument_id="MTX",
            contract_month="202612",
            trade_date=date(2026, 11, 16),
            available_at=signal_at + timedelta(hours=5),
            price=Decimal(101),
        ),
        quantity=1,
        gross_pnl=Decimal(50),
        broker_fee=Decimal(10),
        tax=Decimal(5),
    )
    entry_one = _position_fill("entry-1", date(2026, 11, 16), 30, "sell")
    entry_two = _position_fill("entry-2", date(2026, 11, 17), 30, "buy")
    exit_two = _position_fill("exit-2", date(2026, 11, 17), 60, "sell")
    trades = (
        CompletedTrade(
            trade_id="trade-1",
            entry_trade_date=date(2026, 11, 16),
            trade_date=date(2026, 11, 16),
            session="day",
            gross_pnl=Decimal(1000),
            net_pnl=Decimal(980),
            costs=Decimal(20),
            contract_month="202611",
        ),
        CompletedTrade(
            trade_id="trade-2",
            entry_trade_date=date(2026, 11, 17),
            trade_date=date(2026, 11, 17),
            session="night",
            gross_pnl=Decimal(-500),
            net_pnl=Decimal(-520),
            costs=Decimal(20),
            roll=True,
            contract_month="202612",
        ),
    )
    metrics = calculate_metrics(
        _empty_result(fills=(entry_one, fill, entry_two, exit_two), settlements=(settlement,)),
        initial_equity=Decimal(100000),
        daily_equity=(
            (date(2026, 11, 16), Decimal(100050)),
            (date(2026, 11, 17), Decimal(100000)),
        ),
        completed_trades=trades,
    )

    assert metrics.gross_pnl == Decimal(30)
    assert metrics.broker_fee == Decimal(15)
    assert metrics.tax == Decimal(7)
    assert metrics.slippage == Decimal(3)
    assert metrics.net_pnl == Decimal(5)
    assert metrics.traded_notional == Decimal(20000)
    assert metrics.turnover_ratio == pytest.approx(20000 / (300050 / 3))
    assert metrics.cost_to_gross_pnl == pytest.approx(25 / 30)
    assert metrics.fill_count == 4
    assert metrics.trade_count == 2
    assert metrics.profit_factor == pytest.approx(980 / 520)
    assert metrics.win_rate == 0.5
    assert metrics.max_drawdown == pytest.approx(50 / 100050)
    assert metrics.trade_ledger_status == "unverified_external"
    assert metrics.slices["session:night"]["trade_count"] == 1
    assert metrics.slices["roll:true"]["net_pnl"] == "-520"
    assert metrics.slices["month:2026-11"]["trade_count"] == 2
    assert metrics.slices["contract_month:202612"]["net_pnl"] == "-520"


def test_metrics_leave_return_metrics_unavailable_without_daily_equity() -> None:
    metrics = calculate_metrics(_empty_result(), initial_equity=Decimal(100000))
    zero_trade_metrics = calculate_metrics(
        _empty_result(), initial_equity=Decimal(100000), completed_trades=()
    )

    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.cagr is None
    assert metrics.max_drawdown is None
    assert metrics.trade_count is None
    assert metrics.turnover_ratio is None
    assert metrics.profit_factor is None
    assert zero_trade_metrics.trade_count == 0


def test_metrics_leave_cost_ratio_undefined_when_gross_pnl_is_not_positive() -> None:
    fill = _position_fill("loss", date(2026, 1, 1), 30, "buy", gross_pnl=Decimal(-1))
    metrics = calculate_metrics(
        _empty_result(fills=(fill,)),
        initial_equity=Decimal(1000),
        daily_equity=((date(2026, 1, 1), Decimal(1000)),),
    )

    assert metrics.gross_pnl == Decimal(-1)
    assert metrics.cost_to_gross_pnl is None
    assert metrics.turnover_ratio == pytest.approx(5000 / 1000)


def test_metric_ratios_ignore_external_decimal_precision() -> None:
    fill = _position_fill("precision", date(2026, 1, 1), 30, "buy", gross_pnl=Decimal(3))
    fill = BacktestFill.model_validate(fill.model_dump() | {"broker_fee": Decimal(1)})
    kwargs = {
        "initial_equity": Decimal(1000),
        "daily_equity": ((date(2026, 1, 1), Decimal(2000)),),
    }
    with localcontext() as context:
        context.prec = 4
        low_precision = calculate_metrics(_empty_result(fills=(fill,)), **kwargs)
    with localcontext() as context:
        context.prec = 28
        high_precision = calculate_metrics(_empty_result(fills=(fill,)), **kwargs)

    assert low_precision.turnover_ratio == high_precision.turnover_ratio
    assert low_precision.cost_to_gross_pnl == high_precision.cost_to_gross_pnl


def test_metric_ratios_return_null_when_decimal_values_overflow() -> None:
    tiny = Decimal("1e-999999")
    tiny_equity = calculate_metrics(
        _empty_result(fills=(_position_fill("tiny-equity", date(2026, 1, 1), 30, "buy"),)),
        initial_equity=tiny,
        daily_equity=((date(2026, 1, 1), tiny),),
    )
    huge_cost_fill = _position_fill(
        "huge-cost", date(2026, 1, 1), 30, "buy", gross_pnl=tiny
    )
    huge_cost_fill = BacktestFill.model_validate(
        huge_cost_fill.model_dump() | {"broker_fee": Decimal("1e999999")}
    )
    huge_cost = calculate_metrics(
        _empty_result(fills=(huge_cost_fill,)),
        initial_equity=Decimal(1000),
        daily_equity=((date(2026, 1, 1), Decimal(1000)),),
    )

    assert tiny_equity.turnover_ratio is None
    assert huge_cost.cost_to_gross_pnl is None


@pytest.mark.parametrize(
    ("initial", "daily"),
    [
        (Decimal(1000), (Decimal("-9e999999"), Decimal("-9e999999"))),
        (Decimal("9e999999"), (Decimal("9e999999"), Decimal("9e999999"))),
    ],
)
def test_turnover_overflow_does_not_abort_report(
    initial: Decimal, daily: tuple[Decimal, Decimal]
) -> None:
    metrics = calculate_metrics(
        _empty_result(),
        initial_equity=initial,
        daily_equity=(
            (date(2026, 1, 1), daily[0]),
            (date(2026, 1, 2), daily[1]),
        ),
    )

    assert metrics.turnover_ratio is None


def test_traded_notional_overflow_does_not_abort_report() -> None:
    fill = _position_fill("huge-price", date(2026, 1, 1), 30, "buy")
    fill = BacktestFill.model_validate(
        fill.model_dump() | {"price": Decimal("9e999999"), "multiplier": 50}
    )

    metrics = calculate_metrics(_empty_result(fills=(fill,)), initial_equity=Decimal(1000))

    assert metrics.traded_notional is None
    assert metrics.turnover_ratio is None


def test_completed_trade_validates_calendar_and_weekly_contract_month() -> None:
    with pytest.raises(ValueError, match="contract_month must be YYYYMM"):
        CompletedTrade(
            trade_id="bad-month",
            entry_trade_date=date(2026, 1, 1),
            trade_date=date(2026, 1, 1),
            session="day",
            gross_pnl=Decimal(0),
            net_pnl=Decimal(0),
            costs=Decimal(0),
            contract_month="202613",
        )
    weekly = CompletedTrade(
        trade_id="weekly-month",
        entry_trade_date=date(2026, 1, 1),
        trade_date=date(2026, 1, 1),
        session="day",
        gross_pnl=Decimal(0),
        net_pnl=Decimal(0),
        costs=Decimal(0),
        contract_month="202602W2",
    )
    assert weekly.contract_month == "202602W2"


def test_backtest_fill_rejects_non_positive_multiplier() -> None:
    fill = _position_fill("bad-multiplier", date(2026, 1, 1), 30, "buy")

    for multiplier in (0, -50):
        with pytest.raises(ValueError, match="greater than 0"):
            BacktestFill.model_validate(fill.model_dump() | {"multiplier": multiplier})


def test_report_trade_parser_preserves_optional_contract_month() -> None:
    trades = generate_report._trade_rows(
        {
            "completed_trades": [
                {
                    "trade_id": "trade-1",
                    "entry_trade_date": "2026-01-01",
                    "trade_date": "2026-01-02",
                    "session": "day",
                    "gross_pnl": "1",
                    "net_pnl": "1",
                    "costs": "0",
                    "contract_month": "202602",
                }
            ]
        }
    )

    assert trades is not None
    assert trades[0].contract_month == "202602"


def test_report_marks_missing_contract_month_slice(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "report.json"
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "completed_trades": [
                    {
                        "trade_id": "trade-1",
                        "entry_trade_date": "2026-01-01",
                        "trade_date": "2026-01-02",
                        "session": "day",
                        "gross_pnl": "1",
                        "net_pnl": "1",
                        "costs": "0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_bytes())
    assert "contract_month:202601" not in report["metrics"]["slices"]
    assert any("契約月份切片不完整" in message for message in report["limitations"])


@pytest.mark.parametrize(("covered", "expected"), [(90, 0), (89, 1), (101, 2)])
def test_branch_coverage_gate_enforces_core_threshold(
    tmp_path: Path, covered: int, expected: int
) -> None:
    project = Path(__file__).resolve().parents[2]
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"totals": {"covered_branches": covered, "num_branches": 100}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        (sys.executable, str(project / "scripts" / "check_branch_coverage.py"), str(report)),
        check=False,
        capture_output=True,
        cwd=project,
        text=True,
    )

    assert result.returncode == expected


def test_metrics_reject_duplicate_completed_trade_ids() -> None:
    trade = CompletedTrade(
        trade_id="same-trade",
        entry_trade_date=date(2026, 1, 1),
        trade_date=date(2026, 1, 1),
        session="day",
        gross_pnl=Decimal(1),
        net_pnl=Decimal(1),
        costs=Decimal(0),
    )

    with pytest.raises(ValueError, match="trade IDs must be unique"):
        calculate_metrics(
            _empty_result(),
            initial_equity=Decimal(1000),
            completed_trades=(trade, trade),
        )


def test_metrics_labels_external_completed_trade_rows_unverified() -> None:
    entry = _position_fill("jan-in", date(2026, 1, 5), 30, "buy")
    exit_ = _position_fill("jan-out", date(2026, 1, 5), 60, "sell")
    trade = CompletedTrade(
        trade_id="invented",
        entry_trade_date=date(2026, 4, 2),
        trade_date=date(2026, 4, 2),
        session="night",
        gross_pnl=Decimal(1000),
        net_pnl=Decimal(1000),
        costs=Decimal(0),
        roll=True,
    )

    metrics = calculate_metrics(
        _empty_result(fills=(entry, exit_)),
        initial_equity=Decimal(1000),
        completed_trades=(trade,),
    )

    assert metrics.trade_ledger_status == "unverified_external"
    assert metrics.slices["year:2026"]["net_pnl"] == "1000"


def test_completed_position_cycles_reject_duplicate_fill_execution_identity() -> None:
    entry = _position_fill("same", date(2026, 4, 1), 30, "buy")
    exit_ = _position_fill("same", date(2026, 4, 1), 30, "buy")

    with pytest.raises(ValueError, match="fill execution identities must be unique"):
        count_completed_position_trades(
            _empty_result(fills=(entry, exit_)), date(2026, 4, 1), date(2026, 4, 1)
        )


def test_metrics_reject_duplicate_settlement_identity() -> None:
    record = SettlementRecord(
        event=SettlementEvent(
            instrument_id="MTX",
            contract_month="202612",
            trade_date=date(2026, 11, 16),
            available_at=datetime(2026, 11, 16, 13, 45, tzinfo=TAIPEI),
            price=Decimal(101),
        ),
        quantity=1,
        gross_pnl=Decimal(500),
        broker_fee=Decimal(10),
        tax=Decimal(5),
    )

    with pytest.raises(ValueError, match="settlement event identities must be unique"):
        calculate_metrics(
            _empty_result(settlements=(record, record)), initial_equity=Decimal(1000)
        )


def test_oos_trade_count_excludes_cycle_touched_by_partial_roll() -> None:
    entry = _position_fill("old-in", date(2026, 4, 1), 30, "buy", quantity=2)
    partial_roll = _position_fill(
        "old-roll", date(2026, 4, 1), 60, "sell", reason="roll"
    )
    new_month = _position_fill(
        "new-roll",
        date(2026, 4, 1),
        90,
        "buy",
        contract_month="202701",
        reason="roll",
    )
    old_exit = _position_fill("old-out", date(2026, 4, 2), 30, "sell")
    new_exit = _position_fill(
        "new-out", date(2026, 4, 2), 60, "sell", contract_month="202701"
    )

    assert count_completed_position_trades(
        _empty_result(fills=(entry, partial_roll, new_month, old_exit, new_exit)),
        date(2026, 4, 1),
        date(2026, 4, 2),
    ) == 0


def test_oos_trade_count_comes_from_completed_fill_cycles() -> None:
    old_entry = _position_fill("old-in", date(2026, 3, 30), 30, "buy")
    old_exit = _position_fill("old-out", date(2026, 3, 30), 60, "sell")
    oos_entry = _position_fill("oos-in", date(2026, 4, 1), 30, "buy")
    oos_exit = _position_fill("oos-out", date(2026, 4, 2), 60, "sell")
    open_entry = _position_fill("open-in", date(2026, 4, 2), 90, "sell")
    result = _empty_result(fills=(old_entry, old_exit, oos_entry, oos_exit, open_entry))

    assert count_completed_position_trades(result, date(2026, 4, 1), date(2026, 4, 2)) == 1


def test_oos_trade_count_excludes_roll_legs() -> None:
    entry = _position_fill("entry", date(2026, 4, 1), 30, "buy")
    roll_close = _position_fill(
        "roll-close", date(2026, 4, 2), 30, "sell", reason="roll"
    )
    roll_open = _position_fill(
        "roll-open",
        date(2026, 4, 2),
        31,
        "buy",
        contract_month="202701",
        reason="roll",
    )
    exit_after_roll = _position_fill(
        "exit", date(2026, 4, 3), 30, "sell", contract_month="202701"
    )

    result = _empty_result(fills=(entry, roll_close, roll_open, exit_after_roll))

    assert count_completed_position_trades(result, date(2026, 4, 1), date(2026, 4, 3)) == 0


def test_metrics_include_open_positions_from_last_available_snapshot() -> None:
    at = datetime(2026, 11, 16, 13, 45, tzinfo=TAIPEI)
    snapshot = BacktestSnapshot(
        snapshot_at=at,
        cash=Decimal(10000),
        equity=Decimal(10020),
        margin_used=Decimal(1000),
        positions={"MTX:202612": 1},
        unrealized=Decimal(20),
        maintenance_margin=Decimal(900),
        margin_complete=True,
        gross_pnl=Decimal(0),
        broker_fee=Decimal(0),
        tax=Decimal(0),
        slippage=Decimal(0),
    )
    result = BacktestResult(orders=(), fills=(), snapshots=(snapshot,), settlements=(), audit=())

    metrics = calculate_metrics(result, initial_equity=Decimal(10000))

    assert metrics.realized_gross_pnl == Decimal(0)
    assert metrics.unrealized_pnl == Decimal(20)
    assert metrics.gross_pnl == Decimal(20)
    assert metrics.net_pnl == Decimal(20)
    assert metrics.valuation_at == at
    assert metrics.open_positions == {"MTX:202612": 1}


def test_metrics_preserve_bankruptcy_drawdown_and_skip_undefined_returns() -> None:
    metrics = calculate_metrics(
        _empty_result(),
        initial_equity=Decimal(1000),
        daily_equity=(
            (date(2026, 1, 1), Decimal(1000)),
            (date(2026, 1, 2), Decimal("-133.956")),
        ),
    )

    assert metrics.max_drawdown == pytest.approx(1.133956)
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.cagr is None


def test_return_statistics_start_at_first_daily_close() -> None:
    metrics = calculate_metrics(
        _empty_result(),
        initial_equity=Decimal(10000),
        daily_equity=(
            (date(2026, 1, 1), Decimal(15000)),
            (date(2026, 1, 2), Decimal(15000)),
        ),
    )

    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.cagr == 0


def test_cagr_overflow_keeps_other_metrics_available() -> None:
    metrics = calculate_metrics(
        _empty_result(),
        initial_equity=Decimal(1000),
        daily_equity=(
            (date(2026, 1, 1), Decimal(1000)),
            (date(2026, 1, 2), Decimal(10000)),
        ),
    )

    assert metrics.cagr is None
    assert metrics.max_drawdown == 0


def test_unrepresentable_max_drawdown_is_left_unset() -> None:
    metrics = calculate_metrics(
        _empty_result(),
        initial_equity=Decimal(1000),
        daily_equity=(
            (date(2026, 1, 1), Decimal(1000)),
            (date(2026, 1, 2), Decimal("-1e312")),
        ),
    )

    assert metrics.max_drawdown is None


def test_null_edge_bootstrap_does_not_report_significance() -> None:
    noise = tuple(0.001 if index % 2 == 0 else -0.001 for index in range(200))
    result = paired_block_bootstrap(
        noise,
        (0.0,) * len(noise),
        block_size=2,
        resamples=1_000,
        seed=42,
    )

    assert result.mean_difference == 0
    assert result.lower == result.upper == 0
    assert result.significant is False
    assert result.status == "degenerate_bootstrap"


def test_nonzero_zero_width_bootstrap_interval_is_not_significant() -> None:
    differences = (-0.009, 0.011) * 50
    result = paired_block_bootstrap(
        differences,
        (0.0,) * len(differences),
        block_size=2,
        resamples=1_000,
    )

    assert result.lower == result.upper == pytest.approx(0.001)
    assert result.significant is False
    assert result.status == "degenerate_bootstrap"


def test_constant_nonzero_bootstrap_samples_keep_exact_zero_width() -> None:
    difference = 0.007030652442153011
    result = paired_block_bootstrap(
        (difference,) * 100,
        (0.0,) * 100,
        resamples=1_000,
    )

    assert result.lower == result.upper == difference
    assert result.significant is False
    assert result.status == "degenerate_bootstrap"


def test_bootstrap_is_deterministic_and_marks_small_samples() -> None:
    rng = Random(7)
    strategy = tuple(rng.gauss(0, 0.01) for _ in range(40))
    baseline = tuple(rng.gauss(0, 0.01) for _ in range(40))
    first = paired_block_bootstrap(strategy, baseline, resamples=1_000, seed=19)
    second = paired_block_bootstrap(strategy, baseline, resamples=1_000, seed=19)

    assert first == second
    assert first.status == "insufficient_sample"
    assert first.significant is False


def test_bootstrap_rejects_degenerate_block_and_too_few_resamples() -> None:
    returns = (0.01, -0.01) * 50
    with pytest.raises(ValueError, match="half the sample"):
        paired_block_bootstrap(returns, (0.0,) * len(returns), block_size=100)
    with pytest.raises(ValueError, match="at least 1000"):
        paired_block_bootstrap(returns, (0.0,) * len(returns), resamples=1)


def test_comparison_requires_enough_completed_trades_even_with_many_days() -> None:
    rows = tuple((date(2026, 1, 1) + timedelta(days=index), 0.001, 0.0) for index in range(120))
    result = compare_paired_daily_returns(
        rows,
        strategy_name="strategy",
        baseline_name="baseline",
        resamples=1_000,
        verified_trade_count=99,
    )

    assert result.interval.observations == 120
    assert result.interval.status == "insufficient_sample"
    assert result.interval.significant is False
    assert result.completed_trades == 99


def test_comparison_rejects_unverified_trade_count() -> None:
    rows = tuple((date(2026, 4, 1) + timedelta(days=index), 0.001, 0.0) for index in range(100))
    result = compare_paired_daily_returns(
        rows,
        strategy_name="strategy",
        baseline_name="baseline",
        resamples=1_000,
        verified_trade_count=0,
    )

    assert result.completed_trades == 0
    assert result.interval.status == "insufficient_sample"
    assert result.interval.significant is False


def test_cost_stress_scenarios_and_one_bar_delay() -> None:
    costs = Costs(
        effective_from=date(2026, 1, 1),
        broker_fee_per_contract=Decimal(10),
        tax_rate=Decimal("0.001"),
        slippage_ticks=0,
        final_settlement_fee_per_contract=Decimal(5),
        final_settlement_tax_rate=Decimal("0.001"),
    )
    schedules = Schedules(costs={"MTX": (costs,)}, margins={})
    scenarios = cost_stress_scenarios(schedules)
    bar_start = datetime(2026, 11, 16, 9, tzinfo=TAIPEI)
    feature = FeatureEvent(
        instrument_id="MTX",
        trade_date=date(2026, 11, 16),
        session="day",
        contract_month="202612",
        bar_start=bar_start,
        bar_end=bar_start + timedelta(minutes=5),
        available_at=bar_start + timedelta(minutes=5),
        values={"close": Decimal(100)},
    )
    delayed = delayed_decision_features((feature,))[0]

    assert len(scenarios) == 6
    assert scenarios[0].schedules.costs["MTX"][0].slippage_ticks == 1
    assert scenarios[0].schedules.costs["MTX"][0].broker_fee_per_contract == Decimal("15.0")
    assert scenarios[-1].schedules.costs["MTX"][0].slippage_ticks == 3
    assert scenarios[-1].schedules.costs["MTX"][0].broker_fee_per_contract == Decimal(20)
    assert delayed.available_at == feature.available_at + timedelta(minutes=5)
    assert delayed.values == feature.values


def test_stress_runner_executes_all_cost_and_delay_combinations() -> None:
    costs = Costs(
        effective_from=date(2026, 1, 1),
        broker_fee_per_contract=Decimal(10),
        tax_rate=Decimal("0.001"),
        slippage_ticks=0,
    )
    schedules = Schedules(costs={"MTX": (costs,)}, margins={})
    bar_start = datetime(2026, 11, 16, 9, tzinfo=TAIPEI)
    feature = FeatureEvent(
        instrument_id="MTX",
        trade_date=date(2026, 11, 16),
        session="day",
        contract_month="202612",
        bar_start=bar_start,
        bar_end=bar_start + timedelta(minutes=5),
        available_at=bar_start + timedelta(minutes=5),
        values={"close": Decimal(100)},
    )
    delayed = delayed_decision_features((feature,))[0]
    observed_decisions: list[tuple[datetime, datetime | None]] = []

    def runner(
        _schedules: Schedules,
        bars: tuple[FeatureEvent, ...],
        decisions: tuple[FeatureEvent, ...] | None,
    ) -> BacktestResult:
        observed_decisions.append(
            (bars[0].available_at, decisions[0].available_at if decisions else None)
        )
        return _empty_result()

    runs = run_stress_scenarios(
        schedules,
        (feature,),
        runner,
    )

    assert len(runs) == 12
    assert runs[0].name == "slippage_1_ticks_fee_1.5x"
    assert runs[1].name.endswith("_delay_1_bar")
    assert observed_decisions[:2] == [
        (feature.available_at, None),
        (feature.available_at, feature.available_at + timedelta(minutes=5)),
    ]
    seen: list[datetime] = []

    def callback(row: FeatureEvent, _positions: dict[tuple[str, str], int]) -> tuple[()]:
        seen.append(row.available_at)
        return ()

    filtered = only_delayed_position_decisions(callback, (delayed,))
    assert not tuple(filtered(feature, {}))
    assert tuple(filtered(delayed, {})) == ()
    assert seen == [delayed.available_at]


def test_run_manifest_hashes_inputs_and_configuration(tmp_path: Path) -> None:
    data_path = tmp_path / "input.json"
    data_path.write_text('{"x":1}', encoding="utf-8")
    input_sha = hashlib.sha256(b'{"x":1}').hexdigest()
    data_path.write_text('{"x":2}', encoding="utf-8")
    manifest = build_run_manifest(
        (data_path,),
        config={"threshold": 2},
        seed=42,
        repository=Path(__file__).resolve().parents[2],
        data_checksums=(("input.json", input_sha),),
    )

    assert manifest["data_sha256"][0]["file_name"] == "input.json"  # type: ignore[index]
    assert manifest["data_sha256"][0]["sha256"] == input_sha  # type: ignore[index]
    assert len(manifest["config_sha256"]) == 64
    assert manifest["seed"] == 42


def test_report_cli_marks_missing_research_inputs_and_refuses_overwrite(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "report.json"
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "100000",
                "backtest_result": _empty_result().model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    command = (
        sys.executable,
        str(project / "scripts" / "generate_report.py"),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    )

    subprocess.run(command, check=True, capture_output=True, cwd=project, text=True)
    report_bytes = output_path.read_bytes()
    report = json.loads(report_bytes)
    assert report["claim_status"] == "未證實"
    assert report["metrics"]["trade_count"] is None
    assert any("未提供完整日終權益" in message for message in report["limitations"])
    assert any("重大行情時間區間" in message for message in report["limitations"])

    blocked = subprocess.run(command, check=False, capture_output=True, cwd=project, text=True)
    assert blocked.returncode != 0
    assert "output already exists" in blocked.stderr
    assert output_path.read_bytes() == report_bytes
    weak_threshold = subprocess.run(
        (*command, "--minimum-trades", "1"),
        check=False,
        capture_output=True,
        cwd=project,
        text=True,
    )
    assert weak_threshold.returncode != 0
    assert "minimum-trades at least 100" in weak_threshold.stderr


def test_report_cli_rejects_lone_surrogate_before_creating_output(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "report.json"
    input_data = {
        "name": "\ud800",
        "initial_equity": "100000",
        "backtest_result": _empty_result().model_dump(mode="json"),
    }
    input_path.write_text(json.dumps(input_data), encoding="utf-8")
    command = (
        sys.executable,
        str(project / "scripts" / "generate_report.py"),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    )

    failed = subprocess.run(command, check=False, capture_output=True, cwd=project, text=True)
    assert failed.returncode != 0
    assert "report cannot be encoded as UTF-8" in failed.stderr
    assert not output_path.exists()

    input_data["name"] = "fixed"
    input_path.write_text(json.dumps(input_data), encoding="utf-8")
    subprocess.run(command, check=True, capture_output=True, cwd=project, text=True)
    assert json.loads(output_path.read_bytes())["name"] == "fixed"


def test_report_cli_keeps_bankruptcy_result_and_nulls_undefined_ratios(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "bankruptcy.json"
    output_path = tmp_path / "bankruptcy-report.json"
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": [
                    {"date": "2026-01-01", "equity": "1000"},
                    {"date": "2026-01-02", "equity": "-133.956"},
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["max_drawdown"] == pytest.approx(1.133956)
    assert report["metrics"]["sharpe"] is None
    assert report["metrics"]["sortino"] is None
    assert report["metrics"]["cagr"] is None
    assert any("降至零或負數" in message for message in report["limitations"])


def test_report_cli_preserves_report_when_oos_sample_is_too_small(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "small-oos.json"
    output_path = tmp_path / "small-oos-report.json"
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(6))
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "evaluation_period": "oos",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": [
                    {"date": day.isoformat(), "equity": "1000"} for day in dates
                ],
                "baseline_daily_equity": [
                    {"date": day.isoformat(), "equity": "1000"} for day in dates
                ],
                "paired_daily_returns": [
                    {"date": day.isoformat(), "strategy": 0, "baseline": 0}
                    for day in dates[1:]
                ],
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["comparison"] is None
    assert report["metrics"]["max_drawdown"] == 0
    assert any("少於 100 筆" in message for message in report["limitations"])


def test_report_cli_skips_mismatched_paired_returns(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "mismatched-oos.json"
    output_path = tmp_path / "mismatched-oos-report.json"
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(101))
    flat_equity = [
        {"date": day.isoformat(), "equity": "1000"} for day in dates
    ]
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "evaluation_period": "oos",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": flat_equity,
                "baseline_daily_equity": flat_equity,
                "paired_daily_returns": [
                    {"date": day.isoformat(), "strategy": 0.001, "baseline": 0}
                    for day in dates[1:]
                ],
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["comparison"] is None
    assert any("不一致" in message for message in report["limitations"])


def test_report_cli_accepts_paired_returns_matching_both_equity_paths(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "matched-oos.json"
    output_path = tmp_path / "matched-oos-report.json"
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(101))
    flat_equity = [
        {"date": day.isoformat(), "equity": "1000"} for day in dates
    ]
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "evaluation_period": "oos",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": flat_equity,
                "baseline_daily_equity": flat_equity,
                "paired_daily_returns": [
                    {"date": day.isoformat(), "strategy": 0, "baseline": 0}
                    for day in dates[1:]
                ],
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["comparison"]["interval"]["observations"] == 100
    assert report["comparison"]["interval"]["significant"] is False
    assert report["comparison"]["interval"]["status"] == "insufficient_sample"


def test_report_cli_bootstraps_returns_derived_from_equity(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "rounded-oos.json"
    output_path = tmp_path / "rounded-oos-report.json"
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(101))
    flat_equity = [{"date": day.isoformat(), "equity": "1000"} for day in dates]
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "evaluation_period": "oos",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": flat_equity,
                "baseline_daily_equity": flat_equity,
                "paired_daily_returns": [
                    {
                        "date": day.isoformat(),
                        "strategy": 5e-13 if index % 2 else 9e-13,
                        "baseline": 0,
                    }
                    for index, day in enumerate(dates[1:])
                ],
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["comparison"]["interval"]["mean_difference"] == 0
    assert report["comparison"]["interval"]["significant"] is False


def test_report_cli_labels_external_completed_trades_unverified(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "fake-trades.json"
    output_path = tmp_path / "fake-trades-report.json"
    fake_trade = {
        "entry_trade_date": "2026-01-02",
        "trade_date": "2026-01-02",
        "session": "day",
        "gross_pnl": "1",
        "net_pnl": "1",
        "costs": "0",
    }
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "completed_trades": [
                    {"trade_id": f"claim-{index}", **fake_trade} for index in range(100)
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=False,
        capture_output=True,
        cwd=project,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["trade_ledger_status"] == "unverified_external"
    assert "inference_status" not in report["metrics"]
    assert any("CompletedTrade ledger 未逐筆" in item for item in report["limitations"])


def test_report_is_not_published_when_temporary_write_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    real_named_temporary_file = tempfile.NamedTemporaryFile

    class PartialWriter:
        def __init__(self, **kwargs: object) -> None:
            self.stream = real_named_temporary_file(**kwargs)
            self.name = self.stream.name

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def write(self, content: bytes) -> int:
            self.stream.write(content[:1])
            self.stream.flush()
            raise OSError("simulated disk full")

        def flush(self) -> None:
            self.stream.flush()

        def fileno(self) -> int:
            return self.stream.fileno()

    monkeypatch.setattr(generate_report.tempfile, "NamedTemporaryFile", PartialWriter)

    with pytest.raises(OSError, match="simulated disk full"):
        generate_report._write_new_file(output, b'{"claim_status":"unverified"}')

    assert not output.exists()
    assert not tuple(tmp_path.glob(".report.json.*.tmp"))


def test_report_cli_skips_bootstrap_when_block_exceeds_half_sample(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "large-block.json"
    output_path = tmp_path / "large-block-report.json"
    dates = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(101))
    flat_equity = [{"date": day.isoformat(), "equity": "1000"} for day in dates]
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "evaluation_period": "oos",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": flat_equity,
                "baseline_daily_equity": flat_equity,
                "paired_daily_returns": [
                    {"date": day.isoformat(), "strategy": 0, "baseline": 0}
                    for day in dates[1:]
                ],
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--block-size",
            "51",
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["comparison"] is None
    assert any("block 大於樣本一半" in message for message in report["limitations"])


def test_report_cli_serializes_unrepresentable_drawdown_as_null(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    input_path = tmp_path / "extreme-drawdown.json"
    output_path = tmp_path / "extreme-drawdown-report.json"
    input_path.write_text(
        json.dumps(
            {
                "initial_equity": "1000",
                "backtest_result": _empty_result().model_dump(mode="json"),
                "daily_equity": [
                    {"date": "2026-01-01", "equity": "1000"},
                    {"date": "2026-01-02", "equity": "-1e312"},
                ],
            }
        ),
        encoding="utf-8",
    )

    subprocess.run(
        (
            sys.executable,
            str(project / "scripts" / "generate_report.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ),
        check=True,
        capture_output=True,
        cwd=project,
        text=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["max_drawdown"] is None
    assert any("最大回撤超出支援" in message for message in report["limitations"])
