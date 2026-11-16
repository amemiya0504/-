"""Read-only access to local research artifacts."""

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

app = FastAPI(title="GTTS research artifacts", docs_url=None, redoc_url=None, openapi_url=None)


def root() -> Path:
    return Path(os.environ.get("GTTS_RUNS_DIR", "runs")).resolve()


def artifact(run_id: str, name: str) -> Any:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise HTTPException(404, "run not found")
    base = root()
    path = (base / run_id / name).resolve()
    if not path.is_relative_to(base) or path.parent != base / run_id:
        raise HTTPException(404, "run not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise HTTPException(404, "artifact not found") from None


def object_artifact(run_id: str, name: str) -> dict[str, Any]:
    value = artifact(run_id, name)
    if not isinstance(value, dict):
        raise HTTPException(404, "artifact must be an object")
    return value


def rows(source: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = source.get(name, [])
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise HTTPException(404, f"{name} must be a list of objects")
    return value


def strings(source: dict[str, Any], name: str) -> list[str]:
    value = source.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HTTPException(404, f"{name} must be a list of strings")
    return value


@app.get("/runs")
def runs() -> list[str]:
    base = root()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.is_symlink()
                  and re.fullmatch(r"[A-Za-z0-9_-]+", p.name))


@app.get("/runs/{run_id}/results")
def results(run_id: str) -> dict[str, Any]:
    report = object_artifact(run_id, "report.json")
    source = object_artifact(run_id, "input.json")
    return {
        "report": report,
        "evaluation_period": source.get("evaluation_period"),
        "initial_equity": source.get("initial_equity"),
        "baseline_initial_equity": source.get("baseline_initial_equity"),
        "daily_equity": rows(source, "daily_equity"),
        "baseline_daily_equity": rows(source, "baseline_daily_equity"),
        "baseline_name": source.get("baseline_name", "baseline"),
        "data_gaps": strings(source, "data_gaps"),
        "rolls": rows(source, "rolls"),
    }


@app.get("/runs/{run_id}/trades")
def trades(run_id: str) -> dict[str, Any]:
    source = object_artifact(run_id, "input.json")
    result = source.get("backtest_result", {})
    if not isinstance(result, dict):
        raise HTTPException(404, "backtest_result must be an object")
    completed_trades = source.get("completed_trades")
    if completed_trades is not None and (
        not isinstance(completed_trades, list)
        or not all(isinstance(row, dict) for row in completed_trades)
    ):
        raise HTTPException(404, "completed_trades must be a list of objects")
    return {"completed_trades": completed_trades, "fills": rows(result, "fills")}


@app.get("/runs/{run_id}/features")
def features(run_id: str) -> dict[str, Any]:
    return {"features": rows(object_artifact(run_id, "input.json"), "features")}
