"""Validate one trading date using offline fixtures or explicitly requested sources."""

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import cast

from gtts.data.calendar import TradingCalendar
from gtts.data.ingest import ingest_daily
from gtts.data.schema import TAIPEI
from gtts.data.sources import SourceAdapter
from gtts.data.sources.taifex import TaifexFutures, TaifexInstitutional
from gtts.data.sources.twse import TwseIndex

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DATE = date(2026, 11, 13)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--dry-run", action="store_true", help="Use synthetic fixtures without writes"
    )
    modes.add_argument("--live", action="store_true", help="Fetch official daily reports")
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--calendar", type=Path, default=ROOT / "configs" / "contracts.yaml")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Check for new source revisions")
    args = parser.parse_args()
    trade_date = cast(date | None, args.date)
    if args.live and (trade_date is None or args.database is None):
        parser.error("--live requires --date and --database")
    trade_date = trade_date or FIXTURE_DATE
    calendar = TradingCalendar.from_yaml(args.calendar)
    adapters: tuple[SourceAdapter, ...] = (TaifexFutures(), TaifexInstitutional(), TwseIndex())
    if args.dry_run:
        if args.database is not None or args.refresh:
            parser.error("--database and --refresh require --live")
        fixtures = {
            adapter.endpoint: ROOT / "tests" / "fixtures" / "sources" / f"{adapter.source_id}.json"
            for adapter in adapters
        }

        def fetch_fixture(endpoint: str, timeout: float) -> tuple[bytes, str]:
            return fixtures[endpoint].read_bytes(), "synthetic-v1"

        result = ingest_daily(
            trade_date,
            calendar,
            adapters,
            None,
            fetch=fetch_fixture,
            attempts=1,
            now=lambda: datetime(2026, 11, 13, 18, tzinfo=TAIPEI),
        )
    else:
        result = ingest_daily(trade_date, calendar, adapters, args.database, refresh=args.refresh)
    print(
        json.dumps(
            {
                "mode": "dry-run" if args.dry_run else "live",
                "trade_date": trade_date.isoformat(),
                "record_count": len(result.records),
                "gaps": [gap.model_dump(mode="json") for gap in result.gaps],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(bool(result.gaps))


if __name__ == "__main__":
    raise SystemExit(main())
