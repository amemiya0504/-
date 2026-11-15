# 回測協定

`gtts.engine.backtest.run_backtest(calendar, schedules, initial_cash, bars, ...)` 是離線 TX／MTX 模擬。輸入實際交割月的 5 分 OHLCV `FeatureEvent`、`Decimal` 初始資金、`OrderRequest`／`RollRequest`，以及有來源的 `SettlementEvent`。可用 `on_feature` 產生委託，callback 收到特徵時，可用 `feature.available_at` 作為決策時間。`decision_features` 可另外安排較晚才可得的策略決策事件；每筆必須對應已輸入的完整棒，且只能修改 `available_at`，只觸發 `on_feature_with_positions`，不參與行情盯市或撮合。可用 `before` 限定事件必須發生於截止前，行事曆檢查也會排程到截止前，即使沒有新的完整棒。若只取得開盤證據，可用 `opening_only_bars` 輸入。此路徑僅讀開盤資訊，不產生完整棒特徵。結果分列訂單、成交、資產快照、結算與 audit。

## 共通風控

`run_backtest(..., risk_limits=RiskLimits())` 啟用獨立風控；S1、S2、S3 的策略回測入口會明確傳入此預設。低階帳務回放未傳 `risk_limits` 時保留不設限模式，適合隔離測試帳務情境，不代表策略預設風控。

- 預設每策略最多 1 口 MTX、全組合最多 1 口 MTX；每筆風險上限為當下權益 0.5%，每日權益自當交易日第一筆快照起回撤達 1% 時，停止該日所有新倉。保護停損必須在委託上提供。風險金額使用含滑價進場價、停損距離、實際合約點值與停損預估的單邊費用、稅、滑價估算；跳空或無量造成停損無法按計畫成交時，損失可超過 0.5%。
- 開倉後權益必須保留原始保證金 20% 現金緩衝。這是可由 `RiskLimits.margin_buffer_fraction` 調整的研究預設，不是期交所規定；實際保證金仍依回測時點生效的 `Schedules`。
- 當棒缺漏、資料無效或委託下一合法開盤缺必要證據時，本次回測對該商品與交割月鎖住新倉並留下 audit；停損、減倉與到期處理仍照常執行。日損限制只停止新倉，不會強制平倉。
- 風控模式下拒絕 `RollRequest`，因為換月契約沒有欄位提供新月份的保護停損。低階未設限帳務回放仍可測試換月撮合。

- 時間：`bar_start` 與 `bar_end` 是市場事件時間，`available_at` 是策略最早可用的時間。callback 在完整、合法棒的 `available_at` 被呼叫，訊號須符合 `available_at <= decision_at`。成交用決策後第一個排定的合法 5 分開盤。前棒收盤、訊號與次棒開盤同時間戳時，先處理收盤、訊號，再把成交記為開盤邊界後 1 微秒。這個偏移只表示先後，K 棒無法提供實際逐筆成交時間。相同輸入與決策會得到相同輸出。
- 撮合資料：市場 OHLC 只使用可信的首次市場觀測版本。每個商品、交割月、`bar_start` 只接受一筆 K 棒，重複時整次回測會在執行前失敗。來源有修訂時須先按當時可得資訊做 point-in-time 清理，保留首次可追溯的市場觀測。晚到的修訂不得改寫先前的模擬成交。
- 撮合：日期、時段、掛牌及最後交易日以日曆驗證。開倉、換月、跳空停損、定時退出、到期或保證金減倉等開盤撮合，須有不晚於開盤的 `open_price_available_at`，以及有效的 `open_volume` 與不晚於開盤的 `open_volume_available_at`。價格與容量各有獨立可得時間。缺少必要證據、開盤無量、缺棒或停牌時不成交。棒內容量上限從整棒量扣除已觀測的全部開盤量，與模擬在開盤成交幾口無關。跨休市或掛牌前的時段可等到合法開盤，時段內缺棒不可跳過。換月明確指定舊、新月份，兩腿在同一開盤全額成交或一起拒絕。現有開盤時點資料來自合成資料，尚無可用的授權歷史來源。
- 停損／停利：保護單附在開倉委託上。原始開盤價與含滑價的進場成交價都須嚴格落在已設定的保護價之內，否則拒單。只有觀測到有量的開盤成交事件，才可用其價格觸發新的開盤停損。如果已觀測到交易，但模擬開盤容量不足，未成交部位保留觸發狀態。棒內撮合須有已核證的開盤成交價及正容量，才可辨識越過保護價的高低價是否存在於開盤之後。開盤已越過的同一側門檻不得再拿整棒高低價重試。證據不足則留下 `unknown_intrabar_extrema`。若同棒確認觸及兩側，以停損優先。棒內成交記在該棒結束前 1 微秒，表示觸及先於收盤。保護單同樣扣不利滑價。棒內成交量只提供粗粒度上限，無法驗證實際成交時點、委託簿或撮合順位。
- 定時退出：`OrderRequest.exit_at` 須為進場後同交易時段的合法 5 分 K 開盤。只有開倉成交才排程。若該部位已停損或平倉，排程不再送出退出。退出只減倉，採開盤價加不利滑價。缺棒、無量或部分成交時，記錄 `time_exit_unresolved` 並禁止同商品各交割月新增曝險，直到該部位平倉。日常盯市不會解除限制。當次定時退出不延到後續時段。
- 帳務：用 `Decimal` 且費稅金額不捨入。合約點值取該交易日有效規格。FIFO 買賣按未加滑價的基準價計算 gross P&L；成交價先加不利滑價，實際價乘點值、口數與稅率計稅。現金扣券商總收費、稅與滑價各一次。只有價格時間戳證明開盤價當時可得，才能按該價盯市。及時價格可用於盯市，即使容量缺失或為零而無法撮合。只有完整棒的行情，於 `available_at` 用有成交的收盤價更新盯市。晚到的舊收盤不會回寫先前快照，也不會覆蓋較新的行情。每日用結算價轉未實現為現金損益，並重設持倉基準價。權益等於現金加未實現損益；原始與維持保證金分開記錄，保證金不從現金扣除。
- 日結與風險：日結使用有時間戳的明確結算價。下一個合法開盤先處理在該時點前或同時已公布的結算價。屆時仍缺日結才記 audit 並拒絕新曝險。該合約完全平倉後解除限制，原有 audit 保留。新倉須符合扣除成本後的原始保證金。權益低於維持保證金時，在下一個可執行開盤依量能減倉，剩餘部位繼續等待強平。原始保證金隨設定的生效時間更新。缺成本資料一律不能成交。缺保證金時封鎖開倉，仍允許費稅已知的減倉。
- 到期：禁止在最後一棒新增曝險，現有部位在該棒開盤嘗試強平。若缺價或量而有未平倉，須有官方最終結算價與明確交割費率才能另記現金結算。最後交易時段收盤仍有部位時封鎖新倉。有效的官方最終結算價公布後即可結清該部位。回測結束仍未結清才記 `expiry_unsettled`。委託若將回測延長到下一個合法開盤，引擎也會補排期間的日結期限與到期檢查。連續期貨只能做特徵，不能提供成交或結算價。
- 研究：預先固定 train／validation／最終 OOS 與 purge／embargo；標準化、校準與門檻只用訓練資料估計。缺資料、交易數不足或授權未知時，明確標示「樣本不足／未證實」。

`configs/costs.yaml` 預設留空。依商品填入 `costs` 列，每列包括交易日 `effective_from`、`broker_fee_per_contract`、`tax_rate`、`slippage_ticks`，與到期現金結算欄位 `final_settlement_fee_per_contract`、`final_settlement_tax_rate`。費用欄位填券商每口單邊總報價，避免把已計入的交易經手費及結算費重複加入。按商品填入 `margins` 列，每列包括台北時間 `effective_from`、每口 `initial` 與 `maintenance`。兩類設定按生效時間排序，各列持續有效至下一列。[期交所收費標準](https://www.taifex.com.tw/cht/4/feeSchedules)、[保證金調整公告](https://www.taifex.com.tw/cht/5/margingAdj)可協助核對。現有 golden cases 僅使用人工數值。

開盤事件只驗證當時可得的價格、容量與合約時段。完整棒的高低價、收盤、總量與停牌資訊在 `available_at` 驗證，缺陷只影響該完整棒的棒內撮合、收盤盯市與特徵，並留下 audit。晚到但有效的完整棒可更新收盤估值。只要晚到棒含棒內量，就留下中性 `late_intrabar_bar` 記錄，不回填任何棒內成交或狀態。

## S1 法人貝氏訊號

`gtts.strategies.institutional_bayes` 提供 `opportunity`、`session_outcome`、`fit_institutional`、`decide` 與 `compare_institutional`。研究資料需明確指定每個目標交易日要使用的 MTX 交割月。應事先固定合約選擇，不可看過當日成交量或報酬才選擇。

- 特徵使用前一交易日 TX 三類法人未平倉淨額的變化。每類以更早的 60 筆交易日變化估計平均與標準差，因此完整窗口需要 62 日資料。以決策時點選取修訂版本，保留來源紀錄與採用的可得時間。預設至少延遲至 T+1 日盤開盤。可靠的公開時間可經審核後允許更早取用，未知公開時間仍須遵守 T+1 下限。
- 決策在前一交易日法人資料可得、且目標日首根日盤 5 分 K 如期完成後產生。最早於下一根開盤成交。出場取停損與定時退出的先發情況。ATR 為同契約已完成日盤 K 棒的 14 期 True Range 平均，委託以 `stop_distance` 指定 1.5 倍 ATR。引擎從含滑價的進場成交價計算停損，朝縮小風險方向對齊 tick。原有絕對停損價仍可單獨指定。出場遇跳空或滑價，實際損失仍可能超過預設距離。定時退出安排在日盤末根 5 分 K 開盤，包含到期日縮短時段。
- 方向標籤使用下一日盤可交易的進場與定時退出開盤價，兩側都須有可用的開盤容量。扣除一口 MTX 長方的來回券商費、交易稅與滑價後，淨損益為正才標為正類。標籤採固定持有區間，實際回測另外套用停損與保證金。長方正類的互補機率包含小幅上漲但未覆蓋成本的情況，因此空方也須通過方向期望毛利的成本門檻。
- 依時間排序後，以訓練前段估計平滑 Gaussian Naive Bayes、logistic regression 與條件毛利；後段單獨用於 Platt 校準，剔除跨越校準決策日才可得的標籤。訓練前段預設至少 30 筆且各類至少 2 筆，校準尾段須有兩類結果。所有標籤須在訓練截止前可得。
- 以 0.60／0.40 機率門檻產生長／短方向。期望毛利達決策當下估計來回成本的兩倍才送出一口委託。單純外資淨額變化方向、首棒價格動能及相同特徵的 logistic regression 共用同一撮合、成本與退出規則。比較結果含逐筆成交、資產快照、audit、Brier score 與機率分箱。OOS 校準從同一組回測 K 棒、日曆與成本重算固定區間標籤。現有測試僅用合成資料，真實 OOS 表仍需有合法分鐘行情與經核對的歷史成本。

OOS 決策的成本門檻依該次回放傳入的成本表重算，缺有效成本列時不產生委託。若開盤事件在截止前已可得，可依當時的開盤價與容量成交，即使完整棒在截止後才可得。這種情況只回放開盤，晚到的整棒欄位不參與回放，也不做該筆 OOS 標籤。到期、結算期限等行事曆檢查仍跑到截止前，保留未結清 audit。

## S2 同步基差回歸

`gtts.features.basis.make_basis_point` 僅配對同一交易日、同一 5 分鐘起訖時間的 TAIEX 與 MTX 日盤資料。TAIEX 棒須位於 09:00 至 13:30 現貨時段；MTX 棒須落在合約日盤、對齊 5 分鐘格線、成交量為正且非停牌。排除合約最後交易日。未知股利資料不產生基差點；股利已建模時只能使用決策時已可得的現值。利率及可得時間皆有效時，參考價為 `(S - PV(D)) * exp(r * tau)`，`tau` 由棒結束時計至最後交易日的期貨日盤收盤；利率缺失或晚到時使用 `S - PV(D)`，並將點標成 simplified，不視為完整公允價。

`basis_zscore` 僅用同月份、同 basis kind、決策時已可得且時間早於目前棒的最近 60 點，標準差採樣本估計；重複歷史時間拒算。`gtts.strategies.basis_reversion.compare_basis` 以 `z > 2` 做空 MTX、`z < -2` 做多一口；持倉在 `|z| < 0.5` 平倉，另設 1.5 ATR 停損及日盤末退出。選月只看該決策時間前已可得、日曆有效的合約棒，再選最近到期月份，避免同日較晚資料改寫較早決策。策略另與同資料區間的期貨價格均值回歸比較。基差策略只有 MTX 單腿，屬方向性研究，不是價差套利。

Fubon adapter 目前只把歷史回應保存為 raw candle，並將 `available_at` 設為首次擷取時間。它沒有原始公布時間、開盤價可得時間或開盤量證據，因此不能回填成棒起始可用，也不能單獨支持精確開盤成交。現有 S2 測試使用合成資料；尚未取得 point-in-time TAIEX 盤中來源、確認資料授權或驗證任何真實策略績效。

## S3 波動狀態突破

`gtts.features.volatility.breakout_features` 重用同契約日盤 ATR14，並計算當日以前 20 個連續完整交易日的日內高低幅度／首棒開盤價。以前一個完整交易日的幅度在這 20 日樣本中的 mid-rank percentile 分成兩態：分位數至少 0.8 為高波動，否則為低波動。量能 z-score 以目前時段位置的總成交量，對相同月份、同一日盤時間位置的前 20 個完整日盤樣本計算母體標準差；歷史量能標準差為零或缺任何一日資料時，S3 不交易。所有歷史資料必須在當前棒決策時間已可得。

`gtts.strategies.regime_breakout` 固定交易一口 MTX，且只在高波動狀態、量能 z-score 至少 1 時採取 Donchian 突破。通道使用同一日盤內、緊鄰目前棒以前連續 12 根完整 K 的最高價與最低價；日盤前 11 根不生成通道，第 12 根歷史 K 完整可得後才可計算通道，不跨前一交易日補棒。實際委託另需 ATR14，因此資料不足 14 根時不送單。收盤高於通道上界做多，低於下界做空，下一個合法開盤進場。停損為含滑價成交價反向 1.5 ATR，停利為順向 2 ATR，價格按 tick 朝進場價取整。日盤末次合法開盤退出；合約最後交易日整日拒絕新倉。13:40 才可得的訊號不會在該日 13:40 末次開盤後新開倉。

`donchian_baseline` 使用相同合約、Donchian 規則、ATR14、相對停損／停利、退出、費稅、滑價與回測區間，但不套高波動或量能 z-score 門檻；獨立使用時不需 20 日 regime 歷史。比較器只保留 S3 波動與量能資料皆可計算的共同突破時點，避免 baseline 使用較早或較多的訊號區間。兩者使用同一批 bars 和 cutoff。K 棒無法說明同棒兩側先後時，引擎以停損優先；開盤跳空越過停損則依開盤價加不利滑價成交。這是保守 OHLC 模擬，不代表逐筆成交或委託順位。資料缺棒、缺 OHLCV、session 不符、未完成日盤歷史或到期日會 fail closed。策略及回歸測試只用合成 K 棒；未取得可核證授權行情，未建立 OOS 結果或真實績效。

## Walk-forward 與研究報告

`gtts.research.splits.walk_forward_splits` 接收已排序且唯一的交易日序列。`train_size`、`validation_size`、`oos_size`、`purge_size` 與 `embargo_size` 都以交易日筆數計算。每折固定長度訓練窗，validation 與 OOS 依序向前移動；兩個邊界各排除 `purge_size` 個交易日，OOS 後排除 `embargo_size` 個交易日再開始下一折。呼叫端須依標籤最大持有期預先設定 purge 與 embargo。`fit_standardizer` 只接受訓練列；同一個 `Standardizer` 應套用到 validation 與 OOS，不能重新 fit。

`gtts.research.metrics.calculate_metrics` 由回測 fills 與 settlement records 核算 realized gross P&L 及成本，並把最後可用快照的 unrealized P&L 加入總 gross/net P&L；報告同時保留 `valuation_at` 和未平倉部位。Sharpe 與 Sortino 使用相鄰日終權益報酬，按 252 個交易日年化，Sortino 的最低報酬設為 0；CAGR 使用第一個到最後一個日終權益，最大回撤另以 `initial_equity` 作起始峰值。若權益降至零或負數，保留損益與可超過 100% 的最大回撤，Sharpe、Sortino、CAGR 留空。日終權益必須完整且依序提供。Profit Factor、勝率、交易數，以及年度、日夜盤、是否換月切片由外部 `CompletedTrade` ledger 計算；日期、時段與損益未逐筆對帳 fills／settlement records，因此 `trade_ledger_status` 標示 `unverified_external`，報告也會列出限制。結算事件可代表實際完成部位，但不納入 OOS 獨立交易數；OOS 數量只從唯一 fill execution identity 推導，部位週期內任何部分換月都排除。成本、滑價、曝險及資金使用皆依可核對的回測事件或快照彙總。快照曝險和保證金使用率是事件快照觀測值，不是時間加權估計。

`gtts.research.bootstrap.paired_block_bootstrap` 對同日期策略與 baseline 報酬差做循環區塊 bootstrap，預設 block 為 5 筆、seed 為 `20261116`、2,000 次重抽樣；重抽樣至少 1,000 次，block 不得超過樣本一半。分位數保留相同估計值，零寬度信賴區間標記為退化 bootstrap，不得判顯著。`compare_paired_daily_returns` 至少需要 100 筆 OOS 日報酬及 100 次由回測 fills 推導的完整部位週期才允許標記初步統計比較。完整週期以實際契約部位從零建立並回到零計數；週期內任何 `roll` 成交都排除，重複的 `(fill_id, signal_at, fill_at)` 會拒算。只計入進場與出場交易日都落在該 OOS 報酬日期區間的週期。報告固定標示 `claim_status: 未證實`，達到門檻仍未校正多策略挑選造成的偏誤，OOS 不得用來調整策略門檻。

`run_stress_scenarios` 以同一策略 runner 重跑滑價 1／2／3 ticks、手續費 1.5／2 倍及兩者搭配延遲一根棒的共 12 種情境。延遲情境保留原始執行 bars，只建立 `available_at` 延後 5 分鐘的 `decision_features`。runner 需用 `only_delayed_position_decisions` 包裝 `on_feature_with_positions`，抑制原時點的策略回呼，再把原始 bars 與 delayed features 一起傳給 `run_backtest`；不要設置會在原始 bars 立即送單的 `on_feature`。這樣只延後策略決策，不會改變 OHLC 停損／停利撮合。

報告輸入 JSON 至少含 `initial_equity` 與 `backtest_result`。可選的 `daily_equity` 是 `{date, equity}` 日終權益列；`baseline_daily_equity` 是日期對齊的 baseline 權益列；`completed_trades` 是 `{trade_id, entry_trade_date, trade_date, session, gross_pnl, net_pnl, costs, roll}` 列表，`trade_id` 必須唯一，`trade_date` 是出場交易日，且需滿足 `net_pnl = gross_pnl - costs`；`paired_daily_returns` 是 `{date, strategy, baseline}` 列表，日期須對應日終權益第二列起的相鄰收盤報酬。CLI 會以權益計算的實際報酬進行比較，JSON 報酬僅供一致性檢查。策略與 baseline 報酬都須對應各自權益序列。只有 `evaluation_period: "oos"` 才會計算策略與 baseline 比較；少於 100 筆配對報酬或 `block_size` 大於樣本一半時，仍會產生一般研究報告並省略統計比較。樣本數由回測 fills 所表示的 OOS 期間完整部位週期計算，門檻不可低於 100。最大回撤超出有限浮點數範圍時留空；CLI 先完成 JSON 序列化與 UTF-8 編碼，再將完整內容寫入同目錄暫存檔，最後以不覆寫的原子連結發布。編碼或寫入失敗不會建立半成品輸出檔。CLI 解析輸入與計算 SHA-256 使用同一份 bytes。未提供的統計量會省略並列出缺項。`scripts/generate_report.py --input input.json --output report.json` 會雜湊輸入、設定與 seed，記錄程式 commit；輸出檔已存在時會拒絕覆寫。離線骨架沒有真實授權分鐘資料，也不會自行聲稱任何 OOS 績效。
