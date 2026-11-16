import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from gtts.api.main import app


def test_api_is_read_only_and_rejects_traversal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GTTS_RUNS_DIR", str(tmp_path))
    run = tmp_path / "run-1"
    run.mkdir()
    (run / "input.json").write_text(
        json.dumps(
            {
                "backtest_result": {"fills": []},
                "evaluation_period": "oos",
                "initial_equity": "100000",
                "baseline_initial_equity": "100000",
                "daily_equity": [{"date": "2026-01-02", "equity": "100000"}],
                "baseline_daily_equity": [{"date": "2026-01-02", "equity": "100000"}],
                "data_gaps": ["synthetic fixture"],
            }
        ),
        encoding="utf-8",
    )
    (run / "report.json").write_text(
        json.dumps({"claim_status": "未證實", "evaluation_period": "oos"}), encoding="utf-8"
    )
    async def exercise_api() -> tuple[httpx.Response, ...]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return (
                await client.get("/runs"),
                await client.get("/runs/run-1/results"),
                await client.get("/runs/run-1/trades"),
                await client.get("/runs/run-1/features"),
                await client.post("/runs/run-1/results"),
                await client.get("/runs/..%5csecret/results"),
                await client.get("/runs/missing/results"),
            )

    runs_response, result_response, trades_response, features_response, post_response, traversal, missing = (
        asyncio.run(exercise_api())
    )
    assert runs_response.json() == ["run-1"]
    result = result_response.json()
    assert result["report"]["claim_status"] == "未證實"
    assert result["initial_equity"] == "100000"
    assert result["baseline_initial_equity"] == "100000"
    assert result["data_gaps"] == ["synthetic fixture"]
    assert trades_response.json()["completed_trades"] is None
    assert features_response.json()["features"] == []
    assert post_response.status_code == 405
    assert traversal.status_code == 404
    assert missing.status_code == 404


def test_dashboard_uses_run_dates_and_displays_sample_status(monkeypatch) -> None:
    streamlit = pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    payloads = {
        "/runs": ["run-1"],
        "/runs/run-1/results": {
            "report": {
                "name": "same",
                "evaluation_period": "oos",
                "claim_status": "未證實",
                "comparison": {
                    "completed_trades": 0,
                    "interval": {"status": "insufficient_sample", "observations": 2},
                },
            },
            "evaluation_period": "oos",
            "initial_equity": "1000",
            "baseline_initial_equity": "100",
            "baseline_name": "same",
            "daily_equity": [
                {"date": "2026-01-01", "equity": "1000"},
                {"date": "2026-01-02", "equity": "1010"},
            ],
            "baseline_daily_equity": [
                {"date": "2026-01-01", "equity": "100"},
                {"date": "2026-01-02", "equity": "101"},
            ],
            "data_gaps": [],
            "rolls": [],
        },
        "/runs/run-1/trades": {"completed_trades": [], "fills": []},
        "/runs/run-1/features": {"features": []},
    }

    def get(url: str, timeout: int) -> httpx.Response:
        del timeout
        path = url.removeprefix("http://127.0.0.1:8000")
        return httpx.Response(
            200, json=payloads[path], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", get)
    charts = []
    original_line_chart = streamlit.line_chart

    def capture_chart(data, *args, **kwargs):
        charts.append(data.copy())
        return original_line_chart(data, *args, **kwargs)

    monkeypatch.setattr(streamlit, "line_chart", capture_chart)
    app = Path(__file__).resolve().parents[2] / "src/gtts/dashboard/app.py"
    result = AppTest.from_file(str(app)).run(timeout=15)

    assert not result.exception
    dates = result.get("date_input")
    assert len(dates) == 2
    assert [widget.value.isoformat() for widget in dates] == ["2026-01-01", "2026-01-02"]
    assert [widget.min.isoformat() for widget in dates] == ["2026-01-01", "2026-01-01"]
    assert [widget.max.isoformat() for widget in dates] == ["2026-01-02", "2026-01-02"]
    assert any("insufficient_sample" in str(getattr(element, "value", element)) for element in result), [
        (type(element).__name__, getattr(element, "value", None)) for element in result
    ]
    assert len(charts) == 4
    assert charts[3]["drawdown"].tolist() == [0.0, 0.0]
    charts.clear()
    result.get("multiselect")[0].set_value(["strategy"])
    result.run(timeout=15)
    assert len(charts) == 2


def test_dashboard_refuses_mismatched_oos_markers(monkeypatch) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    report = {
        "name": "strategy",
        "evaluation_period": "validation",
        "claim_status": "未證實",
        "comparison": None,
    }
    payloads = {
        "/runs": ["run-1"],
        "/runs/run-1/results": {
            "report": report,
            "evaluation_period": "oos",
            "daily_equity": [{"date": "2026-01-01", "equity": "1000"}],
            "baseline_daily_equity": [{"date": "2026-01-01", "equity": "1000"}],
            "data_gaps": [],
            "rolls": [],
        },
        "/runs/run-1/trades": {"completed_trades": [], "fills": []},
        "/runs/run-1/features": {"features": []},
    }

    def get(url: str, timeout: int) -> httpx.Response:
        del timeout
        path = url.removeprefix("http://127.0.0.1:8000")
        return httpx.Response(
            200, json=payloads[path], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", get)
    app = Path(__file__).resolve().parents[2] / "src/gtts/dashboard/app.py"
    result = AppTest.from_file(str(app)).run(timeout=15)

    assert not result.exception
    assert not result.get("line_chart")
    assert any("必須一致標記為 OOS" in element.value for element in result.warning)


def test_demo_replays_fixture(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    # The report CLI itself is exercised without writing into the checkout.
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, str(project / "scripts/generate_report.py"), "--input",
                    str(project / "tests/fixtures/dashboard/input.json"), "--output", str(output)],
                   check=True, env=os.environ.copy())
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["claim_status"] == "未證實"
    assert report["evaluation_period"] == "oos"
    assert any("樣本不足" in item for item in report["limitations"])
