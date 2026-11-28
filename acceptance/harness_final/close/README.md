# 核心問題、已送達原件與收尾

普通 `bin/gx-p0 research` 建立每個新 Goal。測試只控制模型傳輸，實際經過 native callbacks、任務／租約、已送達簡報、報告及 SQLite 交易。這組測試檢驗提示是否有事實依據；提示對真人或真模型的幫助程度另需盲評。

| 回執 | 可重查的行為 |
|---|---|
| `pinned-full.log`、`pinned-slim.log` | 在完整站 `4b6aa93` 和精簡站 `6ec604d` 執行同一份七例。兩站均出現提早結題、接受未知／歧義題目及併發交錯等失敗。舊站缺少回執新欄位也以原始錯誤呈現。日誌省略檢出目錄前綴。 |
| `old-full.log`、`old-slim.log` | 保留第一輪三例基線原始結果，之後再加入並發、換題與原件釋出場景。 |
| `scope-facts.log` | 五份讀數中只漏一份核心原件時確實提示。原件已授予子席，Lead 私下理由仍留在自己的作用域。六個停止情境保留原科學措辭，含區間跨零、對照失效、前提不成立及三種行政結束理由。 |
| `question-receipts.json` | 六個具名場景保存實際 Run、事件、產物和裁決。三種根問題寫法、同時提出的收尾、未在報告釋出的子席原件共五次應提醒均命中。子席已讀同一論文的等價段落，且將該原件正式交給 Lead，這次正常結題。這六個場景觀察到零漏提醒與零誤提醒。 |
| `candidate-questions.log` | 七例包含同名意圖的可見 ID 選擇、未釋出的兄弟席題名、同 Run 改 Goal／重啟／新提名，以及回執重送。併發用兩次已準備好的普通回調排出最短交錯；兩者共用一個提示事件，GX 保持 OPEN，之後一筆新裁決生效。 |
| `candidate-combined.log` | 同一次執行涵蓋新案例、舊收尾行為、欄位糾正及關題新鮮度。 |
| `pm25-replay.json`、`candidate-states.json` | 唯讀重放現場 PM₂.₅ Run，含 160 篇候選、41 篇標 OA、9 篇定位讀到全文。將原先關題理由改成不足作反事實檢查，仍為零干預、新 Job 及新回合。另列明確點名個別候選時的讀取、抓取失敗和未取得狀態。 |
| `partial-callback.json` | 研究員的 PARTIAL 報告先發布並喚醒 Lead；同一個原生工具回調取得證據提示，冷重開仍回放原回執。資料庫保持一份報告、一次釋出、一次研究員提示。 |
| `scoped-query-jobs.json` | 三次正式 CPU Job 以凍結快照校驗查詢，保存實際查詢、供應端、分頁與錯誤。Lead 明確點名未完成窗口時收到資訊，後續自行結題。 |
| `gx-partial.json`、`signed-negative.json` | 同一 Run 的兩個席位分別送回系統代交的部分產物和研究員署名的陰性觀測。可沿讀數找到實際 Job 和輸出。 |
| `final-delivery.log` | 十項定向案例通過。父席的正文方法主張若沿用子席的定位讀取，必須引用該席實際釋出的報告和具名原件。 |
| `modules-candidate.log`、`modules-candidate-slim.log` | 完整及精簡發行線各通過六項回調測例。兩次原地升版都用各自真實的前版程式建立 Goal。舊版紅燈及 19 項受影響回歸另見 `modules-*.log`。 |
| `model-grades.json` | 對既有正式預測和雙輸入數值評分的唯讀檢查，可追到兩個獨立 Job 與具名產物。 |

產品沿用原 Run、研究意圖、委派實例及 Join 的身分。每個 Goal 版本各有穩定根問題 ID。關題可用原句、`goal` 或可見 ID。一次 Run 用盡一次打斷後，新資料仍可進簡報。模組可依自身已核對原件交付；Lead 只可沿實際釋出的研究員報告採用該席讀過的同一來源或等價段落。相同文獻識別及原文摘要均須能對上。以先前契約建立的 Goal 沿用原回調格式。

從產品根目錄可執行以下測例。歷史研究的 SQLite 快照及檔案物件需由原研究站提供。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a GX_CODEX_OFFLINE=1 python -B -m unittest -v \
  acceptance.harness_final.close.test_questions acceptance.harness_final.close.test_scope_facts \
  acceptance.harness_final.close.test_module_facts \
  acceptance.harness_refactor.closing.test_closeout acceptance.harness_refactor.closing.test_field_format \
  acceptance.v622.test_close
```

在同一指令環境將 `GX_PREVIOUS_HARNESS_PRODUCT` 設為舊版產品根目錄，便可重驗原地升版；缺少舊檢出時這一例會明確跳過。

指定 `GX_CORE_CLOSE_TRACE` 可寫出六個受控場景的原回執。`test_questions.py` 也可用候選檔案路徑配合基線根目錄的 `PYTHONPATH` 驗證舊站。
