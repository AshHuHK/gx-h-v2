# 核心問題、已送達原件與收尾

普通 `bin/gx-p0 research` 建立每個新 Goal。測試只控制模型傳輸，實際經過 native callbacks、任務／租約、已送達簡報、報告及 SQLite 交易。這組測試檢驗提示是否有事實依據；提示對真人或真模型的幫助程度另需盲評。

| 回執 | 可重查的行為 |
|---|---|
| `pinned-full.log`、`pinned-slim.log` | 在完整站 `4b6aa93` 和精簡站 `6ec604d` 執行同一份七例。兩站均出現提早結題、接受未知／歧義題目及併發交錯等失敗。舊站缺少回執新欄位也以原始錯誤呈現。日誌省略檢出目錄前綴。 |
| `question-receipts.json` | 六個具名場景保存實際 Run、事件、產物和裁決。三種根問題寫法、同時提出的收尾、未在報告釋出的子席原件共五次應提醒均命中。子席已讀同一論文的等價段落，且將該原件正式交給 Lead，這次正常結題。這六個場景觀察到零漏提醒與零誤提醒。 |
| `candidate-questions.log` | 七例包含同名意圖的可見 ID 選擇、未釋出的兄弟席題名、同 Run 改 Goal／重啟／新提名，以及回執重送。併發用兩次已準備好的普通回調排出最短交錯；兩者共用一個提示事件，GX 保持 OPEN，之後一筆新裁決生效。 |
| `candidate-combined.log` | 同一次執行涵蓋新案例、舊收尾行為、欄位糾正及關題新鮮度。 |
| `pm25-replay.json`、`candidate-states.json` | 唯讀重放現場 PM₂.₅ Run，含 160 篇候選、41 篇標 OA、9 篇定位讀到全文。將原先關題理由改成不足作反事實檢查，仍為零干預、新 Job 及新回合。另列明確點名個別候選時的讀取、抓取失敗和未取得狀態。 |
| `model-grades.json` | 對既有正式預測和雙輸入數值評分的唯讀檢查，可追到兩個獨立 Job 與具名產物。 |

產品沿用原 Run、研究意圖、委派實例及 Join 的身分。每個 Goal 版本各有穩定根問題 ID。關題可用原句、`goal` 或可見 ID。一次 Run 用盡一次打斷後，新資料仍可進簡報。模組可依自身已核對原件交付；Lead 只可沿實際釋出的研究員報告採用該席讀過的同一來源或等價段落。相同文獻識別及原文摘要均須能對上。以先前契約建立的 Goal 沿用原回調格式。

從產品根目錄可執行以下測例。歷史研究的 SQLite 快照及檔案物件需由原研究站提供。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a GX_CODEX_OFFLINE=1 python -B -m unittest -v \
  acceptance.harness_final.close.test_questions acceptance.harness_final.close.test_module_facts \
  acceptance.harness_refactor.closing.test_closeout acceptance.harness_refactor.closing.test_field_format \
  acceptance.v622.test_close
```

指定 `GX_CORE_CLOSE_TRACE` 可寫出六個受控場景的原回執。`test_questions.py` 也可用候選檔案路徑配合基線根目錄的 `PYTHONPATH` 驗證舊站。
