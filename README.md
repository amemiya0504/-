# GTTS：賽局理論 × 台指期量化研究系統

目前是 PR-01 的離線骨架，沒有真實行情、策略、回測或下單功能。時間戳採 `Asia/Taipei`；研究訊號只能使用決策時間前已可得的資料。

## 離線 smoke

需要 Python 3.12 與 uv。首次安裝需可取得 Python 套件；之後測試與 smoke 不呼叫外部資料服務。

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv run python scripts/run_smoke.py
```
