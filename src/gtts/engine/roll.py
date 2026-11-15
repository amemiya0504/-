"""Two linked orders for an explicit position roll."""

from gtts.engine.broker import other
from gtts.engine.events import BacktestOrder, RollRequest


def roll_orders(request: RollRequest) -> tuple[BacktestOrder, BacktestOrder]:
    signal = request.signal
    common = {
        "signal_id": signal.signal_id,
        "instrument_id": signal.instrument_id,
        "quantity": request.quantity,
        "order_at": signal.decision_at,
        "reason": "roll",
    }
    return (
        BacktestOrder(
            order_id=f"{signal.signal_id}:close",
            side=other(signal.side),
            contract_month=request.from_contract_month,
            **common,
        ),
        BacktestOrder(
            order_id=f"{signal.signal_id}:open",
            side=signal.side,
            contract_month=request.to_contract_month,
            **common,
        ),
    )
