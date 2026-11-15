"""Inputs and audit records for the futures simulator."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from gtts.data.schema import (
    FillEvent,
    OrderEvent,
    PortfolioSnapshot,
    Schema,
    SignalEvent,
    _taipei_datetime,
)

Contract = tuple[str, str]
Side = Literal["buy", "sell"]
Reason = Literal["signal", "roll", "stop", "target", "time_exit", "margin", "expiry"]


class OrderRequest(Schema):
    signal: SignalEvent
    contract_month: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    stop_distance: Decimal | None = Field(default=None, gt=0)
    target_price: Decimal | None = Field(default=None, gt=0)
    exit_at: datetime | None = None

    @field_validator("exit_at")
    @classmethod
    def exit_uses_taipei(cls, value: datetime | None) -> datetime | None:
        return _taipei_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def validate_bracket(self) -> Self:
        stop, target = self.stop_price, self.target_price
        if stop is not None and self.stop_distance is not None:
            raise ValueError("specify either a stop price or a distance from entry")
        if (
            stop is not None
            and target is not None
            and (not stop < target if self.signal.side == "buy" else not stop > target)
        ):
            raise ValueError("stop and target must be in the correct order for the entry side")
        if self.exit_at is not None and self.exit_at <= self.signal.decision_at:
            raise ValueError("timed exit must be later than the entry decision")
        return self


class RollRequest(Schema):
    signal: SignalEvent
    from_contract_month: str = Field(min_length=1)
    to_contract_month: str = Field(min_length=1)
    quantity: int = Field(gt=0)

    @model_validator(mode="after")
    def different_months(self) -> Self:
        if self.from_contract_month == self.to_contract_month:
            raise ValueError("roll requires two distinct contracts")
        return self


class SettlementEvent(Schema):
    instrument_id: str = Field(min_length=1)
    contract_month: str = Field(min_length=1)
    trade_date: date
    available_at: datetime
    price: Decimal = Field(gt=0)
    final: bool = False

    _use_taipei = field_validator("available_at")(_taipei_datetime)


class BacktestOrder(OrderEvent):
    contract_month: str
    reason: Reason

    @property
    def contract(self) -> Contract:
        return self.instrument_id, self.contract_month


class BacktestFill(FillEvent):
    contract_month: str
    trade_date: date
    reason: Reason
    multiplier: int
    reference_price: Decimal
    gross_pnl: Decimal
    broker_fee: Decimal
    tax: Decimal
    slippage: Decimal

    @property
    def net_cash_change(self) -> Decimal:
        return self.gross_pnl - self.slippage - self.broker_fee - self.tax


class BacktestSnapshot(PortfolioSnapshot):
    unrealized: Decimal
    maintenance_margin: Decimal | None
    margin_complete: bool
    gross_pnl: Decimal
    broker_fee: Decimal
    tax: Decimal
    slippage: Decimal


class SettlementRecord(Schema):
    event: SettlementEvent
    quantity: int
    gross_pnl: Decimal
    broker_fee: Decimal
    tax: Decimal


class Audit(Schema):
    at: datetime
    code: str
    detail: str
    instrument_id: str
    contract_month: str | None = None
    order_id: str | None = None


class BacktestResult(Schema):
    orders: tuple[BacktestOrder, ...]
    fills: tuple[BacktestFill, ...]
    snapshots: tuple[BacktestSnapshot, ...]
    settlements: tuple[SettlementRecord, ...]
    audit: tuple[Audit, ...]
