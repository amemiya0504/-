"""Generate a research-only report from an exported BacktestResult JSON input."""

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from math import isclose, isfinite
from pathlib import Path
from typing import Literal, cast

from gtts.engine.events import BacktestResult
from gtts.research import MINIMUM_INDEPENDENT_TRADES, MINIMUM_RESEARCH_OBSERVATIONS
from gtts.research.comparison import build_run_manifest, compare_paired_daily_returns
from gtts.research.metrics import (
    CompletedTrade,
    ResearchMetrics,
    calculate_metrics,
    count_completed_position_trades,
)

ROOT = Path(__file__).resolve().parents[1]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _date(value: object) -> date:
    return date.fromisoformat(str(value))


def _trade_rows(payload: dict[str, object]) -> tuple[CompletedTrade, ...] | None:
    rows = payload.get("completed_trades")
    if rows is None:
        return None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("completed_trades must be a list of objects")
    return tuple(
        CompletedTrade(
            trade_id=row["trade_id"],
            entry_trade_date=_date(row["entry_trade_date"]),
            trade_date=_date(row["trade_date"]),
            session=cast(Literal["day", "night"], row["session"]),
            gross_pnl=_decimal(row["gross_pnl"]),
            net_pnl=_decimal(row["net_pnl"]),
            costs=_decimal(row["costs"]),
            roll=cast(bool, row.get("roll", False)),
        )
        for row in rows
    )


def _metrics_for(payload: dict[str, object], initial_equity: Decimal) -> ResearchMetrics:
    if "backtest_result" not in payload:
        raise ValueError("each run must contain a backtest_result")
    result = BacktestResult.model_validate(payload["backtest_result"])
    daily_equity = _daily_equity_rows(payload, "daily_equity")
    return calculate_metrics(
        result,
        initial_equity=initial_equity,
        daily_equity=daily_equity,
        completed_trades=_trade_rows(payload),
    )


def _daily_equity_rows(payload: dict[str, object], field: str) -> tuple[tuple[date, Decimal], ...]:
    rows = payload.get(field, [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be a list of objects")
    return tuple((_date(row["date"]), _decimal(row["equity"])) for row in rows)


def _returns_from_equity(
    paired: list[dict[str, object]],
    equity: tuple[tuple[date, Decimal], ...],
    field: Literal["strategy", "baseline"],
) -> tuple[float, ...] | None:
    if len(equity) < 2 or tuple(_date(row["date"]) for row in paired) != tuple(
        day for day, _ in equity[1:]
    ):
        return None
    previous = equity[0][1]
    if not previous.is_finite() or previous <= 0:
        return None
    returns: list[float] = []
    for row, (_, current) in zip(paired, equity[1:], strict=True):
        if not current.is_finite() or current <= 0:
            return None
        try:
            expected = float(current / previous - 1)
            actual = float(_decimal(row[field]))
        except (ArithmeticError, ValueError):
            return None
        if not isfinite(expected) or not isfinite(actual) or not isclose(
            actual, expected, rel_tol=1e-9, abs_tol=1e-12
        ):
            return None
        returns.append(expected)
        previous = current
    return tuple(returns)


def _json_default(value: object) -> str:
    if isinstance(value, (Decimal, date, datetime)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _write_new_file(output: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output.parent, prefix=".gtts-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Research input JSON")
    parser.add_argument("--output", type=Path, required=True, help="New report JSON path")
    parser.add_argument("--seed", type=int, default=20_261_116)
    parser.add_argument("--block-size", type=int, default=5)
    parser.add_argument("--resamples", type=int, default=2_000)
    parser.add_argument("--minimum-trades", type=int, default=MINIMUM_INDEPENDENT_TRADES)
    args = parser.parse_args()
    if (
        args.block_size <= 0
        or args.resamples < 1_000
        or args.minimum_trades < MINIMUM_INDEPENDENT_TRADES
    ):
        parser.error(
            "block-size must be positive, resamples at least 1000, minimum-trades at least 100"
        )

    source = args.input.resolve()
    if not source.is_file():
        parser.error(f"input file does not exist: {source}")
    try:
        input_bytes = source.read_bytes()
        payload = json.loads(input_bytes, parse_float=Decimal)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        parser.error(f"cannot read input JSON: {exc}")
    if not isinstance(payload, dict) or "backtest_result" not in payload:
        parser.error("input must contain a serialized backtest_result object")
    if "initial_equity" not in payload:
        parser.error("input must contain initial_equity")
    result = BacktestResult.model_validate(payload["backtest_result"])
    try:
        daily_equity = _daily_equity_rows(payload, "daily_equity")
        baseline_equity = _daily_equity_rows(payload, "baseline_daily_equity")
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        trades = _trade_rows(payload)
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        parser.error(f"invalid completed_trades: {exc}")
    config = payload.get("config", {})
    if not isinstance(config, dict):
        parser.error("config must be a JSON object")
    initial_equity = _decimal(payload["initial_equity"])
    try:
        metrics = calculate_metrics(
            result,
            initial_equity=initial_equity,
            daily_equity=daily_equity,
            completed_trades=trades,
        )
    except ValueError as exc:
        parser.error(str(exc))
    strategy_count = payload.get("tested_strategy_count", 1)
    if type(strategy_count) is not int or strategy_count <= 0:
        parser.error("tested_strategy_count must be a positive integer")
    limitations = payload.get("limitations", [])
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        parser.error("limitations must be a list of strings")
    manifest = build_run_manifest(
        (source,),
        config={
            **config,
            "report": {
                "block_size": args.block_size,
                "resamples": args.resamples,
                "minimum_trades": args.minimum_trades,
                "minimum_paired_observations": 100,
            },
        },
        seed=args.seed,
        repository=ROOT,
        data_checksums=((source.name, hashlib.sha256(input_bytes).hexdigest()),),
    )
    report: dict[str, object] = {
        "name": str(payload.get("name", source.stem)),
        "claim_status": "未證實",
        "evaluation_period": str(payload.get("evaluation_period", "unspecified")),
        "tested_strategy_count": strategy_count,
        "metrics": asdict(metrics),
        "comparison": None,
        "stress_results": [],
        "run_manifest": manifest,
        "limitations": list(limitations),
    }
    paired = payload.get("paired_daily_returns", [])
    if not isinstance(paired, list) or not all(isinstance(row, dict) for row in paired):
        parser.error("paired_daily_returns must be a list of objects")
    if paired:
        if not daily_equity:
            cast(list[str], report["limitations"]).append(
                "未提供 OOS 日終權益，無法核對配對報酬的資金存續狀態。"
            )
        elif any(equity <= 0 for _, equity in daily_equity):
            cast(list[str], report["limitations"]).append(
                "權益曾降至零或負數，停止 OOS 報酬比較的顯著性判定。"
            )
        elif payload.get("evaluation_period") == "oos":
            if len(paired) < MINIMUM_RESEARCH_OBSERVATIONS:
                cast(list[str], report["limitations"]).append(
                    "OOS 配對日報酬少於 100 筆，保留研究報告但略過統計比較。"
                )
            elif 2 * args.block_size > len(paired):
                cast(list[str], report["limitations"]).append(
                    "bootstrap block 大於樣本一半，保留研究報告但略過統計比較。"
                )
            elif not baseline_equity:
                cast(list[str], report["limitations"]).append(
                    "未提供 baseline 日終權益，無法核對配對報酬；略過統計比較。"
                )
            elif (
                strategy_returns := _returns_from_equity(paired, daily_equity, "strategy")
            ) is None or (
                baseline_returns := _returns_from_equity(paired, baseline_equity, "baseline")
            ) is None:
                cast(list[str], report["limitations"]).append(
                    "配對報酬與策略／baseline 日終權益不一致，略過統計比較。"
                )
            else:
                rows = tuple(
                    (_date(row["date"]), strategy, baseline)
                    for row, strategy, baseline in zip(
                        paired, strategy_returns, baseline_returns, strict=True
                    )
                )
                comparison = compare_paired_daily_returns(
                    rows,
                    strategy_name=str(payload.get("name", "strategy")),
                    baseline_name=str(payload.get("baseline_name", "baseline")),
                    block_size=args.block_size,
                    resamples=args.resamples,
                    seed=args.seed,
                    minimum_observations=MINIMUM_RESEARCH_OBSERVATIONS,
                    verified_trade_count=count_completed_position_trades(
                        result, rows[0][0], rows[-1][0]
                    ),
                    minimum_trades=args.minimum_trades,
                )
                report["comparison"] = asdict(comparison)
                cast(list[str], report["limitations"]).append(
                    "bootstrap 信賴區間未校正多策略挑選；只能作條件式初步比較。"
                )
        else:
            cast(list[str], report["limitations"]).append(
                "比較資料未標記為最終 OOS，因此不產生策略與 baseline 的推論比較。"
            )
    else:
        cast(list[str], report["limitations"]).append(
            "未提供配對的 OOS 日報酬，策略與 baseline 差異不可推論。"
        )
    if not daily_equity:
        cast(list[str], report["limitations"]).append(
            "未提供完整日終權益，Sharpe、Sortino、CAGR 與最大回撤不計算。"
        )
    elif any(equity <= 0 for _, equity in daily_equity):
        cast(list[str], report["limitations"]).append(
            "日終權益曾降至零或負數，Sharpe、Sortino、CAGR 不適用；最大回撤與損益仍保留。"
        )
    if daily_equity and metrics.max_drawdown is None:
        cast(list[str], report["limitations"]).append(
            "最大回撤超出支援的浮點範圍，該欄位留空。"
        )
    if trades is None:
        cast(list[str], report["limitations"]).append(
            "未提供配對完成交易，Profit Factor、勝率、交易數與年度／日夜盤／換月切片不計算。"
        )
    elif not trades:
        cast(list[str], report["limitations"]).append(
            "CompletedTrade ledger 未提供列，交易層級統計與切片為空；不據此判定回測沒有完成交易。"
        )
    else:
        cast(list[str], report["limitations"]).append(
            "外部 CompletedTrade ledger 未逐筆與回測成交／結算對帳，交易層級統計與切片未核證。"
        )
    if manifest["code_dirty"] is True:
        cast(list[str], report["limitations"]).append(
            "產生報告時研究程式有未提交變更，僅靠 commit 無法重現此結果。"
        )
    elif manifest["code_commit"] is None or manifest["code_dirty"] is None:
        cast(list[str], report["limitations"]).append("無法取得完整 Git commit／工作目錄狀態。")
    stress_rows = payload.get("stress_results", [])
    if not isinstance(stress_rows, list) or not all(isinstance(row, dict) for row in stress_rows):
        parser.error("stress_results must be a list of objects")
    report["stress_results"] = [
        {
            "name": str(row["name"]),
            "metrics": asdict(_metrics_for(row, initial_equity)),
        }
        for row in stress_rows
    ]
    if not stress_rows:
        cast(list[str], report["limitations"]).append("未提供成本與延遲壓力情境的回測結果。")
    else:
        expected = {
            f"slippage_{ticks}_ticks_fee_{fee}x{suffix}"
            for ticks in (1, 2, 3)
            for fee in ("1.5", "2")
            for suffix in ("", "_delay_1_bar")
        }
        provided = {str(row.get("name", "")) for row in stress_rows}
        missing = sorted(expected - provided)
        if missing:
            report["missing_stress_scenarios"] = missing
            cast(list[str], report["limitations"]).append(
                "壓力測試未涵蓋全部 12 個成本／延遲情境。"
            )

    output = args.output.resolve()
    try:
        serialized = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
            allow_nan=False,
        )
        serialized_bytes = (serialized + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        parser.error(f"report cannot be encoded as UTF-8: {exc}")
    except (TypeError, ValueError, OverflowError) as exc:
        parser.error(f"report cannot be serialized: {type(exc).__name__}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_new_file(output, serialized_bytes)
    except FileExistsError:
        parser.error(f"output already exists: {output}")
    except OSError as exc:
        parser.error(f"cannot write report: {exc}")
    print(f"report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
