"""Optional pre-trade and post-trade limits for research backtests."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field

from gtts.data.quality import FeatureEvent
from gtts.data.schema import Schema
from gtts.engine.events import BacktestSnapshot, OrderRequest, RollRequest

Request = OrderRequest | RollRequest


class RiskLimits(Schema):
    max_strategy_mtx: int = Field(default=1, gt=0, strict=True)
    max_portfolio_mtx: int = Field(default=1, gt=0, strict=True)
    max_trade_risk_fraction: Decimal = Field(default=Decimal("0.005"), gt=0, le=1)
    max_daily_loss_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    margin_buffer_fraction: Decimal = Field(default=Decimal("0.2"), ge=0, le=1)


class RiskGate:
    """Latch invalid data and daily loss; evaluate proposed risk before execution."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.trade_date: date | None = None
        self.day_open_equity: Decimal | None = None
        self.daily_halted = False
        self.data_halted: set[tuple[str, str]] = set()
        self.last_feature_end: dict[tuple[str, str, str], tuple[date, datetime]] = {}

    def observe(self, snapshot: BacktestSnapshot, trade_date: date) -> bool:
        if self.trade_date is None or trade_date > self.trade_date:
            self.trade_date = trade_date
            self.day_open_equity = snapshot.equity
            self.daily_halted = False
        if (
            not self.daily_halted
            and self.day_open_equity is not None
            and self.day_open_equity > 0
            and snapshot.equity <= self.day_open_equity * (1 - self.limits.max_daily_loss_fraction)
        ):
            self.daily_halted = True
            return True
        return False

    def observe_feature(self, feature: FeatureEvent) -> bool:
        instrument = feature.instrument_id
        month = feature.contract_month or ""
        session = feature.session
        trade_date = feature.trade_date
        key = (instrument, month, session)
        previous = self.last_feature_end.get(key)
        if previous is not None:
            if trade_date < previous[0] or (
                trade_date == previous[0] and feature.bar_end <= previous[1]
            ):
                return False
            if trade_date == previous[0] and previous[1] != feature.bar_start:
                self.last_feature_end[key] = (trade_date, feature.bar_end)
                return True
        self.last_feature_end[key] = (trade_date, feature.bar_end)
        return False

    def observe_open(
        self,
        instrument_id: str,
        contract_month: str,
        session: str,
        trade_date: date,
        bar_start: datetime,
    ) -> bool:
        previous = self.last_feature_end.get((instrument_id, contract_month, session))
        if previous is None or trade_date != previous[0]:
            return False
        return previous[1] < bar_start

    def halt_data(self, instrument_id: str, contract_month: str) -> bool:
        key = (instrument_id, contract_month)
        already_halted = key in self.data_halted
        self.data_halted.add(key)
        return not already_halted

    def blocked_entry(self, instrument_id: str, contract_month: str) -> tuple[str, str] | None:
        if (instrument_id, contract_month) in self.data_halted:
            return "data_kill_switch", "new entries blocked after a data interruption"
        if self.daily_halted:
            return "daily_loss_limit", "daily equity loss limit reached; new entries are blocked"
        return None

    def position_error(
        self,
        request: Request,
        positions: dict[tuple[str, str], int],
        strategy_exposure: int,
    ) -> tuple[str, str] | None:
        if request.signal.instrument_id != "MTX":
            return None
        if strategy_exposure + request.quantity > self.limits.max_strategy_mtx:
            return "strategy_position_limit", "MTX quantity exceeds the per-strategy limit"
        exposure = sum(
            abs(quantity) for (instrument, _), quantity in positions.items() if instrument == "MTX"
        )
        if exposure > self.limits.max_portfolio_mtx:
            return "portfolio_position_limit", "aggregate MTX exposure exceeds the portfolio limit"
        return None

    def trade_risk_error(
        self, risk_amount: Decimal | None, equity: Decimal
    ) -> tuple[str, str] | None:
        if risk_amount is None:
            return "missing_protective_stop", "risk-limited entries require a protective stop"
        if equity <= 0 or risk_amount > equity * self.limits.max_trade_risk_fraction:
            return "trade_risk_limit", "planned stop loss exceeds the per-trade equity limit"
        return None

    def margin_error(self, equity: Decimal, initial_margin: Decimal) -> tuple[str, str] | None:
        required_buffer = initial_margin * self.limits.margin_buffer_fraction
        if equity - initial_margin < required_buffer:
            return (
                "margin_cash_buffer",
                "equity does not preserve the configured margin cash buffer",
            )
        return None
