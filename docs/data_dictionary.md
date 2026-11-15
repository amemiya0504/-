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
| `availability_estimated` | 來源未提供 `published_at` 時為真；下一交易日期貨日盤開盤為可得時間下限 |
| `session` | `day` 或 `night`，按商品彙總的法人資料留空 |
| `participant` | 按商品彙總的法人身分，其他來源留空 |
| `decision_at` | 策略作出訊號決策的時間 |
| `fill_at` | 訂單成交時間；必須晚於所屬訊號 |
| `FeatureEvent.open_price_available_at` | `values["open"]` 從可追溯開盤事件取得的可得時間。須為台北時間且不晚於 `bar_start`。欄位缺失時，開盤價只可視為完整 K 棒的一部分。 |
| `FeatureEvent.open_volume` | 可於該 5 分 K 開盤成交的整口容量。可缺省，`0` 表示無量。 |
| `FeatureEvent.open_volume_available_at` | 開盤容量的可得時間，須為台北時間且不晚於 `bar_start`。此欄位只證明容量時點，開盤價須另有可得時間。 |

`Observation` 沿用 `MarketEvent` 的時間欄位與來源識別。期貨行情的 `event_at` 是該契約交易時段的收盤時間，法人日彙總用期貨日盤收盤時間，TAIEX 用 TWSE 一般交易收盤 13:30。API 未提供可核對的公布時間，所以 `published_at` 留空。`source_revision` 優先使用 HTTP `ETag`，缺少時以 `sha256:` 加上快照 SHA-256 表示。 [TWSE 交易制度](https://www.twse.com.tw/zh/products/system/trading.html)

| 來源 | `values` |
|---|---|
| `taifex-futures` | 單一月／週契約的 `open`、`high`、`low`、`close`、`volume`、`settlement`、`open_interest`。來源的空欄位保留 `null`，價差列略過。 |
| `taifex-institutional` | `trading_volume_net`、`open_interest_net`，口數按商品與法人身分彙總，`contract_month` 留空。 |
| `twse-index` | `close` 為 TAIEX 收盤點數，`market_shares_traded` 為上市市場成交股數。 |

SQLite 以來源識別、端點雜湊、交易日、原始回應 SHA-256 識別快照。新內容保留為新快照，相同內容重用首次取得時間。擷取會重新驗證快照 SHA-256、重新解析並記錄標準化資料與缺口。無缺口的擷取結果才會更新目前快照指標。缺列檢查涵蓋商品／時段、商品／法人身分與日收盤，個別合約的預期掛牌清單仍待建立。

5 分 K 的 `values["volume"]` 是整棒成交量，適用於已完成特徵。開盤價和開盤容量皆須有獨立的可得時間，才能供精確開盤撮合。棒內撮合以整棒成交量扣除觀測開盤量作為粗略上限。缺少經核證的開盤成交價或正容量時，整棒高低價可能包含無法辨識的開盤價，因此不供棒內保護單撮合。目前只有合成 K 棒提供這些開盤欄位，尚無可用的授權歷史來源。

整棒價格、量與停牌欄位的驗證從 `available_at` 生效，無法回寫之前已核證的開盤事件。OOS 截止時只取得開盤事件的列可供開盤撮合與盯市，整棒內容暫不可用於特徵、棒內撮合或研究標籤。

Point-in-time 查詢只納入 `available_at <= decision_at` 的觀測，對同一邏輯事件依 `available_at`、`ingested_at` 選取當時最新修訂；後續修訂不會回寫早期查詢。`audit_observations` 保留完整 raw versions，先依決策截止時間過濾，再對可用版本做 duplicate、gap、outlier 與 session-boundary 檢查；未指定 `decision_at` 時以最大 `available_at` 作目前資料視圖。TAIEX 需為交易日的日盤收盤資料，TX／MTX 法人彙總需對齊交易日日盤收盤。缺少 `published_at` 時仍依 adapter 的 T+1 日盤開盤與首次取得時間較晚者推算，`unknown_publication_delay` 可增加保守延遲，並由 `availability_estimated` 標示。5 分 K 僅在 `available_at >= bar_end` 且整段位於同一 session/trade date 後可進入特徵查詢。這是合成測試驗證的程式契約，並未確認任何盤中資料來源、歷史可追溯性或資料授權。
