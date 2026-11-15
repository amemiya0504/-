"""Risk-priced stop levels shared by execution and pre-trade checks."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from gtts.engine.events import Side


def stop_from_distance(entry: Decimal, side: Side, distance: Decimal, tick: Decimal) -> Decimal:
    if entry <= 0 or distance <= 0 or tick <= 0:
        raise ValueError("entry, stop distance, and tick must be positive")
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    level = entry - distance if side == "buy" else entry + distance
    stop = (level / tick).to_integral_value(rounding=rounding) * tick
    if stop <= 0:
        raise ValueError("stop must be positive")
    return stop


def stop_loss_amount(entry: Decimal, stop: Decimal, quantity: int, multiplier: int) -> Decimal:
    if entry <= 0 or stop <= 0 or quantity < 1 or multiplier < 1:
        raise ValueError("stop loss inputs must be positive")
    return abs(entry - stop) * quantity * multiplier
