# 假設與待確認事項

- 時間戳以帶時區的 `datetime` 儲存，時區為 `Asia/Taipei`。缺少來源公布時間時，`published_at` 留空，`available_at` 以 T+1 交易日日盤開盤與首次取得時間的較晚者為準，並標記為推定。
- `available_at` 表示策略最早能使用該筆資訊的時間；資料事件須滿足 `available_at >= event_at`。策略決策須另外檢查所有輸入 `available_at <= decision_at`。
- 目前 schema 只驗證單筆時間欄位的基本順序，不代表已完成 point-in-time join 或歷史修訂版本稽核。
- 2026-11-14 查核。TX 每點 NT$200、MTX 每點 NT$50，最小升降單位都是一點。`configs/contracts.yaml` 依生效日期儲存規格。日盤 08:45–13:45，夜盤 15:00–次日 05:00，夜盤歸屬下一個日盤的交易日。到期契約最後交易日 13:30 收盤，該契約當晚不開盤。 [TX 規格](https://www.taifex.com.tw/cht/2/tX)、[MTX 規格](https://www.taifex.com.tw/cht/2/mTX)、[盤後規則](https://www.taifex.com.tw/cht/4/aHIntroduction)、[盤後制度沿革](https://www.taifex.com.tw/cht/4/historyOfTrading)
- 夜盤時段依歸屬的交易日選用生效規格，夜盤取消例外以原定開盤的日曆日期設定。
- 日曆涵蓋 2026 年，包含當年春節、例假與已列入的颱風休市公告。春節前 2 月 11 日夜盤屬 2 月 23 日，TX／MTX `202602` 最後交易日也順延至 2 月 23 日。年度表以外的新公告可用休市、加開日盤、取消夜盤及契約到期例外欄位更新，覆蓋範圍外會回報資料缺口。 [2026 年行事曆](https://www.taifex.com.tw/file/taifex/CHINESE/4/2026Calendar.pdf)、[開休市日期表](https://www.taifex.com.tw/file/taifex/CHINESE/11/attach/%E5%8F%B0%E6%9C%9F%E4%BA%A4%E5%AD%97%E7%AC%AC1140003036%E8%99%9F%E5%87%BD.pdf)、[春節契約異動](https://www.taifex.com.tw/file/taifex/CHINESE/11/attach/%E5%8F%B0%E6%9C%9F%E4%BA%A4%E5%AD%97%E7%AC%AC1140202563%E8%99%9F%E5%87%BD.pdf)、[期交所公告](https://www.taifex.com.tw/cht/11/announcement)
- TAIFEX 日行情與三大法人、TWSE 大盤日收盤來源已核對官方 OpenAPI。政府資料開放平台列示這三組日資料適用政府資料開放授權條款第 1 版。程式讀取 JSON 回應，其他格式記錄為缺口。TODO：確認可回溯範圍、來源歷史修訂規則與各份日報的公布時間。 [TAIFEX OpenAPI](https://openapi.taifex.com.tw/)、[TWSE OpenAPI](https://openapi.twse.com.tw/)、[期貨行情資料集](https://data.gov.tw/dataset/11319)、[三大法人資料集](https://data.gov.tw/dataset/11596)、[大盤日資料集](https://data.gov.tw/dataset/11672)
- TODO：驗證歷史回測期間內的臨時休市、日夜盤異動及新契約實際掛牌日。日曆的契約時段代表一般規則與設定中的到期例外，沒有完整的掛牌清單或逐筆交易狀態。
- 盤中同步指數、5 分 K、成交量的歷史來源、使用權與發布時間尚未確認。沒有授權或可追溯 fixture 時不得宣稱已完成真實歷史回測。
- 尚未提供資金、券商費率、稅費、滑價、保證金及換月資料；不產生績效數字。
