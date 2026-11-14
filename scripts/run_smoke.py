"""Construct and validate one synthetic market event without network access."""

from datetime import date, datetime

from gtts.data.schema import TAIPEI, MarketEvent

event = MarketEvent(
    source_id="synthetic",
    instrument_id="MTX",
    contract_month="202612",
    trade_date=date(2026, 11, 13),
    event_at=datetime(2026, 11, 13, 9, 0, tzinfo=TAIPEI),
    published_at=None,
    ingested_at=datetime(2026, 11, 13, 9, 1, tzinfo=TAIPEI),
    available_at=datetime(2026, 11, 13, 9, 1, tzinfo=TAIPEI),
    source_revision="synthetic-v1",
    checksum="offline-only",
)
assert event.available_at >= event.event_at
print("GTTS offline schema smoke passed")
