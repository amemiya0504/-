"""Enforce the branch coverage target for core execution, data and risk code."""

import json
import sys
from pathlib import Path

MINIMUM_BRANCH_COVERAGE = 90.0


def main() -> int:
    report_path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("coverage.json")
    try:
        totals = json.loads(report_path.read_text(encoding="utf-8"))["totals"]
        covered = totals["covered_branches"]
        total = totals["num_branches"]
        if (
            type(covered) is not int
            or type(total) is not int
            or total <= 0
            or covered < 0
            or covered > total
        ):
            raise ValueError("coverage report has invalid branch totals")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"invalid coverage report: {error}", file=sys.stderr)
        return 2
    percent = 100 * covered / total
    print(f"Core branch coverage: {covered}/{total} ({percent:.1f}%)")
    if percent < MINIMUM_BRANCH_COVERAGE:
        print(f"required: at least {MINIMUM_BRANCH_COVERAGE:.0f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
