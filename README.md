# GTTS：賽局理論 × 台指期量化研究系統

專案提供台指期研究的時間契約、TX／MTX 交易日曆、TAIFEX／TWSE 日資料擷取，以及離線期貨回測核心。時間戳採 `Asia/Taipei`，研究訊號只能使用決策時間前已可得的資料。策略與券商下單不在目前範圍。

## 離線 smoke

需要 Python 3.12 與 uv。首次安裝需可取得 Python 套件；之後測試與 smoke 不呼叫外部資料服務。

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv run python scripts/run_smoke.py
uv run python scripts/ingest_daily.py --dry-run
```

`--dry-run` 使用 2026-11-13 的合成 fixture，列出筆數與資料缺口，不連網、不寫檔。有缺口時回傳結束碼 `1`。交易所休市日回傳零筆，超出日曆覆蓋範圍則回報缺口。

## 日資料擷取

```powershell
uv run python scripts/ingest_daily.py --live --date 2026-11-13 --database data/gtts.sqlite
```

`--live` 呼叫官方日資料 OpenAPI，從回傳結果選取指定交易日，並將資料存入指定的 SQLite。日期參數用於選取資料，官方日資料 OpenAPI 的可回溯範圍尚未確認。一般重跑會優先讀取無缺口的快照。加上 `--refresh` 會重新擷取，有缺口時保留上次可用的快取。SHA-256 相同的原始快照保留首次取得時間。缺少來源公開時間時，`available_at` 取下一交易日的期貨日盤開盤與首次取得時間兩者的較晚者。

日曆覆蓋期間與來源、資料欄位、已知缺口見 [假設與待確認事項](docs/assumptions.md) 和 [資料字典](docs/data_dictionary.md)。

離線模擬的撮合、結算、費稅與保證金輸入見 [回測協定](docs/backtest_protocol.md)。成本與保證金設定預設留空，回歸測試使用人工數值，沒有歷史績效結果。
