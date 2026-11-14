# 資料字典

所有時間戳須為帶時區的 `Asia/Taipei` `datetime`。JSON 輸入應包含 offset；schema 另以 `timezone` 欄位明確標示市場時區。

| 欄位 | 說明 |
|---|---|
| `source_id` | 來源識別碼 |
| `instrument_id` | 商品／指數識別碼 |
| `contract_month` | 期貨契約月份，非期貨資料可空 |
| `trade_date` | 交易所定義的交易日，不等同夜盤的日曆日期 |
| `event_at` | 市場事件發生時間 |
| `published_at` | 來源公開時間；未知時為空且需採保守延遲 |
| `ingested_at` | 系統取得資料時間 |
| `available_at` | 策略最早可使用時間 |
| `source_revision` | 來源修訂版本 |
| `checksum` | 原始內容校驗值 |
| `decision_at` | 策略作出訊號決策的時間 |
| `fill_at` | 訂單成交時間；必須晚於所屬訊號 |

`MarketEvent` 目前是來源資料事件的最小 schema。其他事件以其自身事件時間記錄；尚未建立跨事件資料可得性連接或原始資料稽核流程。
