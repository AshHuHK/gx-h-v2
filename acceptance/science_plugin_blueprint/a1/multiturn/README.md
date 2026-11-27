# 同一個普通 Goal 的多輪科研更新

此案作為機制驗收，與 A1 的兩個首次選法案例分開計分。問題和原始輸入只說明模擬的雙站流量試驗、要回答的問題以及三筆待到資料。PI 問題沒有方法名。兩份流量與站點登記皆為作者構造的數位材料，適用範圍限此試驗。

在首個模型回合前凍結兩試驗材料、派工條件、站點登記和各自摘要。初始 Goal 僅帶第一個試驗。普通 GX 部署安裝兩個獨立 CPU 插件。新版本的首份簡報只給作用域和可選方法數量。Lead 自行決定要查的方法、是否派 Job、如何寫下暫定結論，以及收到證據後如何處置。

三筆變更都走公開入口。首次采用決定之後，`assign-material` 指向該決定，送達新試驗。其研究查詢與決定入帳後，`message` 用同一 Run 和上一筆決定作為收件地址，給出實際派工門檻。PI 更新經新研究決定處理後，`observe` 補入含原始 logger SHA-256 的站點歸屬紀錄。後者是本研究的數位觀測，未注入任何工具失敗。

```sh
WORK=../release-acceptance/science-plugin-blueprint/a1
python -B acceptance/science_plugin_blueprint/a1/operate.py prepare multiturn "$WORK"
python -B acceptance/science_plugin_blueprint/a1/operate.py run multiturn "$WORK"
```

在另一個終端以相同 WORK 執行，按原訂依賴送達三筆輸入。流程憑已有 Run、決定與查詢事件前進。沒有多開任何嘗試去替換首輪失敗。

```sh
python -B acceptance/science_plugin_blueprint/a1/multiturn/stage.py "$WORK/multiturn"
python -B acceptance/science_plugin_blueprint/a1/multiturn/index_series.py "$WORK/multiturn" /tmp/pond-native.json
```

## 現場結果

2026-11-27 UTC，原 Run `gxr_01m8f6ktvmbrk2xzns` 正式結題。七個已回執的宿主回合包含 PI Main 對話。開題時整份研究 context 都沒有兩個方法名，`capability_view` 只有 revision 0 和五個選項的數量。Lead 首次用科學需求查詢；邊際方法列第一，能回傳配對時段及差值界限的方法列第二，Lead 自行選了第二個。

| 研究狀態 | 真目錄查詢 → 生效決定 | 實際效果 |
| --- | --- | --- |
| 開題 | #19 → #37 | 在 POND-6 派出 `gxj_01m8f6nttg7dr86yf5`，初步優先考慮北站。 |
| 第二試驗入場 #59 | #66 → #84 | 將具名新原件交給第二個 Job `gxj_01m8f6se4zv8jrddvy`，保留第一案的讀數。 |
| 新 PI 調度門檻 #110 | #131 → #149 | 正式採納逐試驗的嚴格下界門檻，兩案均暫緩派員。 |
| 完整站點歸屬登記 #154 | #160 → #177 | Lead 讀回新登記，核對第二測試的原件摘要及南北站鍵，維持暫緩派員，#179 正式結題。 |

POND-6 共測 20 min，North 2,400 L、South 1,200 L，差值 1,200 L，界限為 960 至 1,440 L。POND-7 共測 30 min，兩站分別為 4,080 L、3,480 L，差值 600 L，界限為 240 至 960 L。逐分鐘獨立復算兩份具名輸出的六個欄位，誤差均為零。第一案的區間跨過 1,000 L 門檻，第二案的上界也低於門檻。

重送最後一筆觀測回到同一個 #154，Artifact 仍為 `gxa_01m8f724wc3f9qfhmc`。終局使用原 Run 重開正常入口，新增零宿主回合及零 Job。三次外部變更在同一個真 Run 都走完了收件、重新查詢及生效決定。冷重啟後重播未確認工具回覆的故障位置仍由另行受控測試覆蓋。

首輪執行中記錄了九次被拒的正式決定草稿。原因包括 local 名稱重複、缺少假設欄位、PI disposition 欄位與值域錯誤、引用尚未送達本回合的具名讀數。修正後均沿用原研究。該 Run 的編號、上下文快照、查詢原話、拒絕、產物、獨立核算與連到結論的事件見 [模型實跑與原始物件索引](native-evidence.json) 和 [結題決定](DECISION.md)。
