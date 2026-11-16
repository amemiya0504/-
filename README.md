# GTTS：賽局理論 × 台指期量化研究系統

專案提供台指期研究的時間契約、TX／MTX 交易日曆、TAIFEX／TWSE 日資料擷取、Fubon 期貨歷史 5 分 K raw adapter、離線期貨回測，以及 S1 法人貝氏、S2 同步基差、S3 波動狀態突破研究策略。時間戳採 `Asia/Taipei`，研究訊號只能使用決策時已可得的資料。系統限於研究與紙上模擬。

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

S1 的資料、校準、成本與退出規則見 [S1 法人貝氏訊號](docs/backtest_protocol.md#s1-法人貝氏訊號)。可跑 `uv run pytest tests/unit/test_institutional.py` 重現合成資料的 OOS 流程。

Fubon 歷史 K 線端點自 SDK v2.4.0 起提供。用 `uv sync --extra fubon` 安裝選配 SDK 後，可執行下列唯讀擷取命令；執行時會隱藏詢問登入資訊，並把指定月契約的單一交易日 5 分 K 存成新的 JSONL 快照：

```powershell
uv run --extra fubon python scripts/ingest_fubon.py --date 2026-11-13 --contract-month 202611 --product TXF --session regular --certificate D:\fadacai\fbs\your-certificate.p12
```

本機 `D:\fadacai\fbs` 內有 2.2.8 與 2.5.0 的 Windows SDK wheel；歷史行情端點需要 SDK 2.4.0 以上，因此擷取器使用 2.5.x。僅有 wheel 不代表已登入或成功取得真實行情。擷取資料只標示 API 首次回傳時間，沒有歷史發布時間或可核證的開盤事件；可供收盤後研究，不能證明原始 K 棒開盤時已可用。現有 fixture 與策略測試仍是合成資料，尚未建立真實績效。S2 的公式、資料時點與限制見 [S2 同步基差回歸](docs/backtest_protocol.md#s2-同步基差回歸)。

S3 使用前 20 個完整日盤估計波動分位數與同時段量能 z-score，只在高波動且放量突破時入場；其 Donchian baseline 不套這兩個篩選，但沿用相同風險、成本及退出規則。兩者資料需求、收盤訊號至下一開盤成交、到期拒單與合成資料限制見 [S3 波動狀態突破](docs/backtest_protocol.md#s3-波動狀態突破)。
