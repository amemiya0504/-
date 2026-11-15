"""Cash, FIFO futures cost bases, and margin requirements."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from gtts.engine.events import BacktestFill, BacktestSnapshot, Contract, SettlementEvent, Side
from gtts.risk.margin import Schedules

ZERO = Decimal(0)


def signed(side: Side, quantity: int) -> int:
    return quantity if side == "buy" else -quantity


@dataclass
class Portfolio:
    cash: Decimal
    lots: dict[Contract, list[tuple[int, Decimal]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    marks: dict[Contract, tuple[Decimal, int]] = field(default_factory=dict)
    gross: Decimal = ZERO
    fees: Decimal = ZERO
    tax: Decimal = ZERO
    slippage: Decimal = ZERO

    def quantity(self, contract: Contract) -> int:
        return sum(quantity for quantity, _ in self.lots.get(contract, ()))

    @property
    def positions(self) -> dict[Contract, int]:
        return {key: self.quantity(key) for key in sorted(self.lots) if self.lots[key]}

    @property
    def unrealized(self) -> Decimal:
        return sum(
            (
                (self.marks[key][0] - basis) * quantity * self.marks[key][1]
                for key in self.positions
                for quantity, basis in self.lots[key]
            ),
            ZERO,
        )

    def mark(self, contract: Contract, price: Decimal, multiplier: int) -> None:
        self.marks[contract] = price, multiplier

    def trade(
        self, contract: Contract, delta: int, reference_price: Decimal, multiplier: int
    ) -> Decimal:
        lots = self.lots[contract]
        pnl = ZERO
        while delta and lots and (lots[0][0] > 0) != (delta > 0):
            quantity, basis = lots[0]
            closed = min(abs(quantity), abs(delta)) * (1 if quantity > 0 else -1)
            pnl += (reference_price - basis) * closed * multiplier
            delta += closed
            if closed == quantity:
                lots.pop(0)
            else:
                lots[0] = (quantity - closed, basis)
        if delta:
            lots.append((delta, reference_price))
        return pnl

    def apply(self, fills: Iterable[BacktestFill]) -> None:
        for fill in fills:
            self.cash += fill.net_cash_change
            self.gross += fill.gross_pnl
            self.fees += fill.broker_fee
            self.tax += fill.tax
            self.slippage += fill.slippage

    def settle(
        self, event: SettlementEvent, multiplier: int, broker_fee: Decimal, tax: Decimal
    ) -> Decimal:
        key = (event.instrument_id, event.contract_month)
        pnl = sum(
            ((event.price - basis) * quantity * multiplier for quantity, basis in self.lots[key]),
            ZERO,
        )
        quantity = self.quantity(key)
        self.lots[key] = [] if event.final else [(quantity, event.price)]
        self.mark(key, event.price, multiplier)
        self.cash += pnl - broker_fee - tax
        self.gross += pnl
        self.fees += broker_fee
        self.tax += tax
        return pnl

    def margin(self, schedules: Schedules, at: datetime) -> tuple[Decimal, Decimal | None]:
        initial = maintenance = ZERO
        complete = True
        for (instrument, _), quantity in self.positions.items():
            row = schedules.margin_at(instrument, at)
            if row is None:
                complete = False
            else:
                initial += row.initial * abs(quantity)
                maintenance += row.maintenance * abs(quantity)
        return initial, maintenance if complete else None

    def snapshot(self, schedules: Schedules, at: datetime) -> BacktestSnapshot:
        initial, maintenance = self.margin(schedules, at)
        unrealized = self.unrealized
        return BacktestSnapshot(
            snapshot_at=at,
            cash=self.cash,
            unrealized=unrealized,
            equity=self.cash + unrealized,
            margin_used=initial,
            maintenance_margin=maintenance,
            margin_complete=maintenance is not None,
            positions={f"{key[0]}:{key[1]}": quantity for key, quantity in self.positions.items()},
            gross_pnl=self.gross,
            broker_fee=self.fees,
            tax=self.tax,
            slippage=self.slippage,
        )
