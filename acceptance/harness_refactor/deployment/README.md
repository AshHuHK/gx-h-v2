# 精簡版部署與獨立驗收

第一次盲評時的完整產品候選為 `5904a4f`，精簡部署提交為 `75174ed`。完整線與精簡線各用自己的 Python、普通 CLI、站點插件註冊和研究狀態。兩線的共同變更涵蓋 63 個逐位元相同的產品檔案，另外核對精簡線原有的註冊項及這次加入的提供者。兩份安裝計算出的契約摘要一致，SQLite DDL 摘要維持原值。[逐檔比對](release-comparison.json)

| 檢查 | 實際結果 |
|---|---|
| 現役精簡安裝，新 Harness 套件 | 80/80。首輪的故障注入假件少了 runner 觀測中的 state，已補回實際契約欄位，重跑整組通過。[首輪紀錄](slim-first-regressions.log) · [修正後](slim-harness-regressions.log) |
| 現役精簡安裝，受影響的舊入口 | 121/121，含普通 research、重開、資料來源、配方和具名輸出。[回歸紀錄](slim-legacy.log) |
| 獨立精簡原始碼匯出與既有回歸 | 288/288，無跳過。該次分母採用匯出當下的測試集合，與上述兩組有重疊。[獨立輸出](slim-export-regressions.log) |
| 真實官方訂閱與精簡 CLI | USGS 單站題一個 Lead 回合結題，兩個日間差值均與獨立 Decimal 計算相符。冷重開新增 Run 事件、Job 和模型回合各 0。[啟動、原件、報告及冷重導](slim-streamflow-observation/README.md) |

使用者部署時提供官方宿主二進位、登入資料及本站已登記的選用資料庫。套件內的 USGS JSON 可直接作為輸入。歷史酵母可查閱四個具名、帶摘要的資料庫快照。個人 OAuth 憑據及 operator 的工作目錄留在各自部署環境。[可攜夾具、宿主支援與資料摘要](portable-fixtures.json)

要從兩個工作樹重算目前部署比對，可在完整研發根目錄執行

```bash
.venv/bin/python -B acceptance/harness_refactor/integration/deployment_check.py \
  . ../science-assets-slim 5904a4fdac3b0267a80fc7b59d3c00427a4871d4
```

## 單席位交付與結題修補的部署

精簡產品提交為 `30b41cab6a2f4167ea90cb6a7a3c9dcc422f9c61`，對應的完整產品提交為 `249d4b6dea96c6200380bc30cba814eaa4336a9a`。包括定向晚件、同一研究任務的席位延用及結題格式指引。可將上面比對命令最後的提交換成此 SHA 重算。第一次盲評和原先部署的檢查保留其各自版本。[最終逐檔與契約比對](release-comparison-249d4b6.json) · [精簡線最終回歸](slim-regressions-249d4b6.log) · [用最終版本冷重開原單站 Run](slim-cold-249d4b6.json)

## 最後發行的研究入口

完整版 `891a9e887b29d94b1ce1d38cbbbed2ef4954869b`，精簡版 `66d1871248daf23ecd3ef8c774466ab37a33b81a`。此次追加對「已讀私件」的準確回覆及舊 Goal 文字相容，正式產品的其餘差異與上一項相同。兩線分別通過 92/92、92/92 項研究入口回歸。精簡的原單站研究冷重開後仍有 32 個本題事件，結題事件編號為 33，新增事件、Job、決策與模型回合均為零。[最終逐檔比對](release-comparison-891a9e8.json) · [精簡線日誌](slim-regressions-891a9e8.log) · [同 Run 的冷重導](slim-cold-891a9e8.json)
