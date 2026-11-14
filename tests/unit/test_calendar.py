from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from gtts.data.calendar import CalendarGap, TradingCalendar
from gtts.data.schema import TAIPEI


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_yaml(Path("configs/contracts.yaml"))


def test_weekend_and_lunar_new_year_nights_use_next_trade_date(calendar: TradingCalendar) -> None:
    friday = calendar.session_at("TX", datetime(2026, 11, 13, 15, tzinfo=TAIPEI))
    saturday = calendar.session_at("TX", datetime(2026, 11, 13, 21, tzinfo=UTC))
    assert friday == saturday
    assert friday and friday.trade_date == date(2026, 11, 16)
    assert calendar.session_at("TX", datetime(2026, 11, 14, 5, 0, 1, tzinfo=TAIPEI)) is None

    holiday_eve = calendar.session_at("MTX", datetime(2026, 2, 12, 1, tzinfo=TAIPEI))
    assert holiday_eve and holiday_eve.trade_date == date(2026, 2, 23)
    assert not calendar.is_trade_date(date(2026, 2, 12))
    assert calendar.session_at("MTX", datetime(2026, 2, 12, 16, tzinfo=TAIPEI)) is None


def test_expiry_applies_to_exact_contract(calendar: TradingCalendar) -> None:
    day = date(2026, 2, 23)
    assert calendar.last_trade_date("TX", "202602") == day
    expiry = calendar.day_session("TX", day, "202602")
    active = calendar.day_session("TX", day, "202603")
    preceding_night = calendar.night_session("TX", day, "202602")
    assert expiry and expiry.closes_at.time() == time(13, 30)
    assert active and active.closes_at.time() == time(13, 45)
    assert preceding_night and preceding_night.opens_at.date() == date(2026, 2, 11)
    stamp = datetime(2026, 2, 23, 16, tzinfo=TAIPEI)
    assert calendar.session_at("TX", stamp, "202602") is None
    assert calendar.session_at("TX", stamp, "202603") is not None
    assert calendar.last_trade_date("MTX", "202602W2") == date(2026, 2, 11)
    with pytest.raises(ValueError, match="unsupported weekly"):
        calendar.last_trade_date("TX", "202602W2")


def test_calendar_override_and_effective_dates_are_data_driven(calendar: TradingCalendar) -> None:
    values = calendar.model_dump()
    changed = values["contracts"]["TX"][0] | {
        "effective_from": date(2026, 7, 1),
        "multiplier_ntd": 250,
    }
    values["contracts"]["TX"] += (changed,)
    values["cancelled_night_starts"] = {date(2026, 7, 1)}
    values["closed_dates"] |= {date(2026, 7, 15)}
    custom = TradingCalendar.model_validate(values)
    assert custom.spec("TX", date(2026, 6, 30)).multiplier_ntd == 200
    assert custom.spec("TX", date(2026, 7, 1)).multiplier_ntd == 250
    assert custom.night_session("TX", date(2026, 7, 2)) is None
    assert custom.last_trade_date("TX", "202607") == date(2026, 7, 16)


def test_night_uses_spec_effective_on_its_trade_date(calendar: TradingCalendar) -> None:
    values = calendar.model_dump()
    values["contracts"]["TX"] += (
        values["contracts"]["TX"][0]
        | {"effective_from": date(2026, 7, 1), "night_open": time(16), "night_close": time(4)},
    )
    custom = TradingCalendar.model_validate(values)
    before = custom.night_session("TX", date(2026, 6, 30))
    changed = custom.night_session("TX", date(2026, 7, 1))
    assert before and before.opens_at == datetime(2026, 6, 29, 15, tzinfo=TAIPEI)
    assert before.closes_at == datetime(2026, 6, 30, 5, tzinfo=TAIPEI)
    assert changed and changed.opens_at == datetime(2026, 6, 30, 16, tzinfo=TAIPEI)
    assert changed.closes_at == datetime(2026, 7, 1, 4, tzinfo=TAIPEI)
    for session in (before, changed):
        assert custom.session_at("TX", session.opens_at) == session
        assert custom.session_at("TX", session.closes_at) == session
    assert custom.session_at("TX", datetime(2026, 6, 30, 15, tzinfo=TAIPEI)) is None
    assert custom.session_at("TX", datetime(2026, 7, 1, 5, tzinfo=TAIPEI)) is None


def test_calendar_does_not_guess_outside_coverage(calendar: TradingCalendar) -> None:
    with pytest.raises(CalendarGap, match="coverage missing"):
        calendar.next_trade_date(date(2026, 12, 31))
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.session_at("TX", datetime(2026, 11, 13, 9))


def test_session_at_distinguishes_break_from_night_at_end_of_coverage(
    calendar: TradingCalendar,
) -> None:
    assert calendar.session_at("TX", datetime(2026, 12, 31, 14, tzinfo=TAIPEI)) is None
    with pytest.raises(CalendarGap, match="coverage missing"):
        calendar.session_at("TX", datetime(2026, 12, 31, 15, tzinfo=TAIPEI))
