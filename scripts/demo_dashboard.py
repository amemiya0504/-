"""Replay a synthetic research fixture into a new run directory."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
runs = project / "runs"
runs.mkdir(exist_ok=True)
run = Path(tempfile.mkdtemp(prefix="synthetic-", dir=runs))
shutil.copyfile(project / "tests/fixtures/dashboard/input.json", run / "input.json")
subprocess.run(
    [sys.executable, str(project / "scripts/generate_report.py"), "--input", str(run / "input.json"),
     "--output", str(run / "report.json")], check=True,
)
print(f"Synthetic run: {run.name}")
