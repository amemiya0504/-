"""Fetch and validate one Fubon TXF/MXF five-minute futures session."""

import argparse
import getpass
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from gtts.data.calendar import TradingCalendar
from gtts.data.schema import TAIPEI
from gtts.data.sources.fubon import FubonFuturesCandles, save_jsonl

ROOT = Path(__file__).resolve().parents[1]


def _sdk_class() -> tuple[Any, type[Exception]]:
    try:
        from importlib.metadata import version

        installed = version("fubon-neo")
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", installed)
        if match is None or tuple(map(int, match.groups())) < (2, 4, 0):
            raise RuntimeError("Fubon historical futures candles require SDK >= 2.4.0")
        from fubon_neo.sdk import FubonSDK
        from fugle_marketdata import FugleAPIError
    except ImportError as error:
        raise RuntimeError("Install the Fubon extra with: uv sync --extra fubon") from error
    return FubonSDK, FugleAPIError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--contract-month", required=True, help="Explicit YYYYMM contract")
    parser.add_argument("--product", choices=("TXF", "MXF"), default="TXF")
    parser.add_argument("--session", choices=("regular", "afterhours"), default="regular")
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--calendar", type=Path, default=ROOT / "configs" / "contracts.yaml")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "fubon")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{6}", args.contract_month):
        parser.error("--contract-month must be YYYYMM")
    if not args.certificate.is_file():
        parser.error("--certificate must point to a local certificate file")

    try:
        FubonSDK, FugleAPIError = _sdk_class()
    except RuntimeError as error:
        parser.error(str(error))

    login_id = getpass.getpass("Fubon login ID: ").strip()
    password = getpass.getpass("Fubon password: ")
    certificate_password = getpass.getpass("Certificate password, blank if none: ")
    if not login_id or not password:
        parser.error("Fubon login ID and password are required")

    sdk = FubonSDK()
    logged_in = False
    try:
        login_args = [login_id, password, str(args.certificate)]
        if certificate_password:
            login_args.append(certificate_password)
        try:
            login_result = sdk.login(*login_args)
        finally:
            login_args.clear()
            password = certificate_password = ""
        if not login_result.is_success:
            print(json.dumps({"status": "login_failed"}, ensure_ascii=False))
            return 1
        logged_in = True
        sdk.init_realtime()
        try:
            historical = sdk.marketdata.rest_client.futopt.historical
        except AttributeError as error:
            raise RuntimeError(
                "This SDK has no futures historical endpoint; install fubon-neo >= 2.4.0"
            ) from error
        calendar = TradingCalendar.from_yaml(args.calendar)
        candles = FubonFuturesCandles(historical, calendar).read(
            args.product,
            args.contract_month,
            args.date,
            args.session,
        )
        stamp = datetime.now(TAIPEI).strftime("%Y%m%dT%H%M%S%z")
        path = args.output_dir / (
            f"{args.product}_{args.contract_month}_{args.date}_{args.session}_{stamp}.jsonl"
        )
        save_jsonl(path, candles)
        print(
            json.dumps(
                {"status": "saved", "path": str(path), "candle_count": len(candles)},
                ensure_ascii=False,
            )
        )
        return 0
    except (FugleAPIError, OSError, RuntimeError, ValueError) as error:
        detail: dict[str, str | int] = {
            "status": "fetch_failed",
            "error_type": type(error).__name__,
        }
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int):
            detail["http_status"] = status_code
        print(json.dumps(detail, ensure_ascii=False))
        return 1
    finally:
        if logged_in:
            try:
                sdk.logout()
            except (FugleAPIError, OSError, RuntimeError, ValueError) as error:
                print(json.dumps({"status": "logout_failed", "error_type": type(error).__name__}))
        try:
            sdk.shutdown()
        except (FugleAPIError, OSError, RuntimeError, ValueError) as error:
            print(json.dumps({"status": "shutdown_failed", "error_type": type(error).__name__}))


if __name__ == "__main__":
    raise SystemExit(main())
