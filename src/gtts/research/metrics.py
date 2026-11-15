"""Metrics that only use explicitly supplied daily and completed-trade observations."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Context, Decimal, localcontext
from itertools import pairwise
from math import isfinite, sqrt
from statistics import stdev
from typing import Literal

from gtts.engine.events import BacktestResult

ZERO = Decimal(0)


@dataclass(frozen=True)
class CompletedTrade:
    trade_id: str
    entry_trade_date: date
    trade_date: date
    session: Literal["day", "night"]
    gross_pnl: Decimal
    net_pnl: Decimal
    costs: Decimal
    roll: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.trade_id, str) or not self.trade_id.strip():
            raise ValueError("trade_id must be non-empty")
        if self.session not in {"day", "night"}:
            raise ValueError("session must be day or night")
        if type(self.roll) is not bool:
            raise ValueError("roll must be a boolean")
        if self.entry_trade_date > self.trade_date:
            raise ValueError("a completed trade must exit on or after its entry trade date")
        amounts = (self.gross_pnl, self.net_pnl, self.costs)
        if not all(isinstance(value, Decimal) and value.is_finite() for value in amounts):
            raise ValueError("completed trade amounts must be finite")
        with localcontext(Context(prec=50)):
            if self.costs < ZERO or self.net_pnl != self.gross_pnl - self.costs:
                raise ValueError(
                    "completed trade net_pnl must equal gross_pnl minus non-negative costs"
                )


@dataclass(frozen=True)
class ResearchMetrics:
    valuation_at: datetime | None
    open_positions: dict[str, int]
    realized_gross_pnl: Decimal
    unrealized_pnl: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    broker_fee: Decimal
    tax: Decimal
    slippage: Decimal
    total_cost: Decimal
    fill_count: int
    filled_contracts: int
    trade_count: int | None
    profit_factor: float | None
    win_rate: float | None
    sharpe: float | None
    sortino: float | None
    cagr: float | None
    max_drawdown: float | None
    daily_observations: int
    exposure_fraction: float | None
    mean_capital_utilization: float | None
    max_capital_utilization: float | None
    trade_ledger_status: str
    slices: dict[str, dict[str, str | int | float]]


def _validate_unique_fills(result: BacktestResult) -> None:
    identities = tuple((fill.fill_id, fill.signal_at, fill.fill_at) for fill in result.fills)
    if len(set(identities)) != len(identities):
        raise ValueError("fill execution identities must be unique")


def completed_position_cycles(result: BacktestResult) -> tuple[tuple[date, date], ...]:
    """Count non-roll flat-to-flat contract positions from actual fills."""
    _validate_unique_fills(result)
    positions: dict[tuple[str, str], int] = {}
    entries: dict[tuple[str, str], tuple[date, bool]] = {}
    cycles: list[tuple[date, date]] = []
    for _, fill in sorted(
        enumerate(result.fills), key=lambda item: (item[1].fill_at, item[0])
    ):
        contract = fill.instrument_id, fill.contract_month
        before = positions.get(contract, 0)
        signed_quantity = fill.quantity if fill.side == "buy" else -fill.quantity
        after = before + signed_quantity
        if before == 0 and after != 0:
            entries[contract] = (fill.trade_date, fill.reason == "roll")
        elif before != 0 and after == 0:
            entry_date, touched_roll = entries.pop(contract, (fill.trade_date, True))
            if not touched_roll and fill.reason != "roll":
                cycles.append((entry_date, fill.trade_date))
        elif before * after < 0:
            entry_date, touched_roll = entries.pop(contract, (fill.trade_date, True))
            touched_roll = touched_roll or fill.reason == "roll"
            if not touched_roll:
                cycles.append((entry_date, fill.trade_date))
            entries[contract] = (fill.trade_date, fill.reason == "roll")
        elif before != 0 and fill.reason == "roll":
            entry_date, _ = entries.get(contract, (fill.trade_date, True))
            entries[contract] = (entry_date, True)
        if after:
            positions[contract] = after
        else:
            positions.pop(contract, None)
    return tuple(cycles)


def count_completed_position_trades(
    result: BacktestResult, start: date, end: date
) -> int:
    if start > end:
        raise ValueError("completed trade date window is reversed")
    return sum(
        start <= entry <= exit <= end for entry, exit in completed_position_cycles(result)
    )


def _validate_daily_equity(
    daily_equity: tuple[tuple[date, Decimal], ...], initial_equity: Decimal
) -> tuple[tuple[date, Decimal], ...]:
    if (
        not isinstance(initial_equity, Decimal)
        or not initial_equity.is_finite()
        or initial_equity <= ZERO
    ):
        raise ValueError("initial_equity must be positive")
    if tuple(day for day, _ in daily_equity) != tuple(sorted({day for day, _ in daily_equity})):
        raise ValueError("daily_equity dates must be unique and chronological")
    if any(not isinstance(equity, Decimal) or not equity.is_finite() for _, equity in daily_equity):
        raise ValueError("daily equity values must be finite")
    return daily_equity


def _slices(trades: tuple[CompletedTrade, ...]) -> dict[str, dict[str, str | int | float]]:
    grouped: dict[str, list[CompletedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[f"year:{trade.trade_date.year}"].append(trade)
        grouped[f"session:{trade.session}"].append(trade)
        grouped[f"roll:{str(trade.roll).lower()}"].append(trade)
    with localcontext(Context(prec=50)):
        return {
            key: {
                "trade_count": len(rows),
                "gross_pnl": str(sum((row.gross_pnl for row in rows), ZERO)),
                "net_pnl": str(sum((row.net_pnl for row in rows), ZERO)),
                "costs": str(sum((row.costs for row in rows), ZERO)),
                "win_rate": sum(row.net_pnl > ZERO for row in rows) / len(rows),
            }
            for key, rows in sorted(grouped.items())
        }


def calculate_metrics(
    result: BacktestResult,
    *,
    initial_equity: Decimal,
    daily_equity: tuple[tuple[date, Decimal], ...] = (),
    completed_trades: tuple[CompletedTrade, ...] | None = None,
) -> ResearchMetrics:
    """Summarize a backtest; annualized metrics require a verified daily equity series."""
    daily = _validate_daily_equity(daily_equity, initial_equity)
    trades = completed_trades or ()
    _validate_unique_fills(result)
    settlement_ids = tuple(
        (row.event.instrument_id, row.event.contract_month, row.event.trade_date)
        for row in result.settlements
    )
    if len(set(settlement_ids)) != len(settlement_ids):
        raise ValueError("settlement event identities must be unique")
    if len({trade.trade_id for trade in trades}) != len(trades):
        raise ValueError("completed trade IDs must be unique")
    if tuple(trade.trade_date for trade in trades) != tuple(
        sorted(trade.trade_date for trade in trades)
    ):
        raise ValueError("completed trades must be in chronological order")
    pnl_amounts = tuple(fill.gross_pnl for fill in result.fills) + tuple(
        row.gross_pnl for row in result.settlements
    )
    costs_amounts = tuple(
        value for fill in result.fills for value in (fill.broker_fee, fill.tax, fill.slippage)
    ) + tuple(value for row in result.settlements for value in (row.broker_fee, row.tax))
    if not all(value.is_finite() for value in (*pnl_amounts, *costs_amounts)) or any(
        value < ZERO for value in costs_amounts
    ):
        raise ValueError("backtest P&L and cost amounts must be finite with non-negative costs")

    final_snapshot = (
        max(
            enumerate(result.snapshots),
            key=lambda item: (item[1].snapshot_at, item[0]),
        )[1]
        if result.snapshots
        else None
    )
    unrealized = final_snapshot.unrealized if final_snapshot is not None else ZERO
    if not unrealized.is_finite():
        raise ValueError("final snapshot unrealized P&L must be finite")
    with localcontext(Context(prec=50)):
        realized_gross = sum((fill.gross_pnl for fill in result.fills), ZERO) + sum(
            (row.gross_pnl for row in result.settlements), ZERO
        )
        broker_fee = sum((fill.broker_fee for fill in result.fills), ZERO) + sum(
            (row.broker_fee for row in result.settlements), ZERO
        )
        tax = sum((fill.tax for fill in result.fills), ZERO) + sum(
            (row.tax for row in result.settlements), ZERO
        )
        slippage = sum((fill.slippage for fill in result.fills), ZERO)
        costs = broker_fee + tax + slippage
        gross = realized_gross + unrealized
        net = gross - costs

    equity_path = (initial_equity,) + tuple(equity for _, equity in daily)
    peak = equity_path[0]
    max_drawdown_ratio = ZERO
    for equity in equity_path[1:]:
        peak = max(peak, equity)
        max_drawdown_ratio = max(max_drawdown_ratio, (peak - equity) / peak)
    try:
        max_drawdown = float(max_drawdown_ratio)
    except OverflowError:
        max_drawdown = None
    if max_drawdown is not None and not isfinite(max_drawdown):
        max_drawdown = None

    daily_path = tuple(equity for _, equity in daily)
    returns = (
        tuple(float(equity / previous - 1) for previous, equity in pairwise(daily_path))
        if all(equity > ZERO for equity in equity_path)
        else ()
    )
    if not all(isfinite(value) for value in returns):
        raise ValueError("daily returns exceed supported numeric range")
    sharpe = None
    if len(returns) >= 2 and stdev(returns) > 0:
        candidate = sqrt(252) * (sum(returns) / len(returns)) / stdev(returns)
        sharpe = candidate if isfinite(candidate) else None
    downside = sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns)) if returns else 0
    sortino = None
    if downside > 0:
        candidate = sqrt(252) * (sum(returns) / len(returns)) / downside
        sortino = candidate if isfinite(candidate) else None
    cagr = None
    if (
        len(daily) >= 2
        and all(equity > ZERO for equity in equity_path)
        and daily[-1][0] > daily[0][0]
    ):
        elapsed_days = (daily[-1][0] - daily[0][0]).days
        if elapsed_days > 0:
            try:
                ratio = float(daily[-1][1] / daily[0][1])
                candidate = ratio ** (365.2425 / elapsed_days) - 1
            except OverflowError:
                candidate = float("inf")
            cagr = candidate if isfinite(candidate) else None

    net_trade_pnls = tuple(trade.net_pnl for trade in trades)
    with localcontext(Context(prec=50)):
        net_wins = sum((pnl for pnl in net_trade_pnls if pnl > ZERO), ZERO)
        net_losses = -sum((pnl for pnl in net_trade_pnls if pnl < ZERO), ZERO)
        candidate = float(net_wins / net_losses) if net_losses else 0.0
    profit_factor = candidate if net_losses and isfinite(candidate) else None
    win_rate = (
        sum(pnl > ZERO for pnl in net_trade_pnls) / len(net_trade_pnls) if net_trade_pnls else None
    )

    snapshots = result.snapshots
    if any(
        not snapshot.equity.is_finite()
        or not snapshot.margin_used.is_finite()
        or snapshot.margin_used < ZERO
        for snapshot in snapshots
    ):
        raise ValueError("snapshot equity and margin must be finite with non-negative margin")
    exposure = (
        sum(bool(snapshot.positions) for snapshot in snapshots) / len(snapshots)
        if snapshots
        else None
    )
    utilization = tuple(
        float(snapshot.margin_used / snapshot.equity)
        for snapshot in snapshots
        if snapshot.margin_complete and snapshot.equity > ZERO
    )
    if not all(isfinite(value) for value in utilization):
        raise ValueError("capital utilization exceeds supported numeric range")
    return ResearchMetrics(
        valuation_at=final_snapshot.snapshot_at if final_snapshot is not None else None,
        open_positions=dict(final_snapshot.positions) if final_snapshot is not None else {},
        realized_gross_pnl=realized_gross,
        unrealized_pnl=unrealized,
        gross_pnl=gross,
        net_pnl=net,
        broker_fee=broker_fee,
        tax=tax,
        slippage=slippage,
        total_cost=costs,
        fill_count=len(result.fills),
        filled_contracts=sum(fill.quantity for fill in result.fills),
        trade_count=len(trades) if completed_trades is not None else None,
        profit_factor=profit_factor,
        win_rate=win_rate,
        sharpe=sharpe,
        sortino=sortino,
        cagr=cagr,
        max_drawdown=max_drawdown if daily else None,
        daily_observations=len(daily),
        exposure_fraction=exposure,
        mean_capital_utilization=sum(utilization) / len(utilization) if utilization else None,
        max_capital_utilization=max(utilization) if utilization else None,
        trade_ledger_status="unverified_external" if completed_trades is not None else "not_provided",
        slices=_slices(trades),
    )
