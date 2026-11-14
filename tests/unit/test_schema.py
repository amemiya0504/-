from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from gtts.data.schema import TAIPEI, FillEvent, MarketEvent, SignalEvent


def market_event(**overrides: object) -> MarketEvent:
    values: dict[str, object] = {
        "source_id": "fixture",
        "instrument_id": "MTX",
        "trade_date": date(2026, 11, 13),
        "event_at": datetime(2026, 11, 13, 9, tzinfo=TAIPEI),
        "published_at": None,
        "ingested_at": datetime(2026, 11, 13, 9, 1, tzinfo=TAIPEI),
        "available_at": datetime(2026, 11, 13, 9, tzinfo=TAIPEI),
    }
    values.update(overrides)
    return MarketEvent.model_validate(values)


def test_market_event_accepts_taipei_aware_times() -> None:
    assert market_event().timezone == "Asia/Taipei"


def test_market_event_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        market_event(event_at=datetime(2026, 11, 13, 9))


def test_market_event_rejects_non_taipei_offset() -> None:
    with pytest.raises(ValidationError, match="Asia/Taipei"):
        market_event(event_at=datetime(2026, 11, 13, 1, tzinfo=UTC))


def test_market_event_rejects_available_before_event() -> None:
    with pytest.raises(ValidationError, match="available_at must be at or after event_at"):
        market_event(available_at=datetime(2026, 11, 13, 8, 59, tzinfo=TAIPEI))


def test_signal_rejects_data_unavailable_at_decision() -> None:
    with pytest.raises(ValidationError, match="available by decision_at"):
        SignalEvent(
            signal_id="s1",
            strategy_id="fixture",
            instrument_id="MTX",
            side="buy",
            decision_at=datetime(2026, 11, 13, 9, tzinfo=TAIPEI),
            available_at=datetime(2026, 11, 13, 9, 1, tzinfo=TAIPEI),
        )


def test_fill_must_happen_after_signal() -> None:
    with pytest.raises(ValidationError, match="fill_at must be later than signal_at"):
        FillEvent(
            fill_id="f1",
            order_id="o1",
            signal_id="s1",
            instrument_id="MTX",
            side="buy",
            quantity=1,
            price="22000",
            signal_at=datetime(2026, 11, 13, 9, tzinfo=TAIPEI),
            fill_at=datetime(2026, 11, 13, 9, tzinfo=TAIPEI),
        )
