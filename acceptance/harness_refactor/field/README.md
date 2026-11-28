# 官方 Codex 現場研究

三份題面均從 `bin/gx-p0 research` 進場。酵母題沒有附件，水文題附上 USGS 原始回應及可供既有工具讀取的區間記錄。PI 只給問題、材料與研究強度。站點配置決定可供選用的能力，Codex 決定是否查閱、局部啟用與採用各方法。兩個水文插件的版本列在 [部署回執](hydro/deployment.json)。[題面、輸入摘要和事前評分](preregistered.json)

## 啟動或恢復

在產品 checkout 執行以下命令。三項共用同一現場目錄。首次開跑會使用現有官方 ChatGPT OAuth 的訂閱額度。啟動器檢查兩個 Clash 監聽並清除模型供應商 API 變數，獨立保存工作狀態及私有 Codex 會話。原始憑據留在帳戶憑據庫。

```bash
.venv/bin/python -B acceptance/harness_refactor/field/run.py yeast ../harness-refactor-live --timeout 1200
.venv/bin/python -B acceptance/harness_refactor/field/run.py a ../harness-refactor-live --timeout 1200
.venv/bin/python -B acceptance/harness_refactor/field/run.py b ../harness-refactor-live --timeout 1200
```

精簡發行可將環境變數 `GX_P0_CODEX` 指到機器上已安裝的官方可執行檔。啟動器會先依本版 `release-assets/lock.json` 核對 SHA-256。完整發行預設使用內附的官方二進位。

每條命令以穩定鍵找到原 Goal。若達到單段 Driver 時限而研究仍開放，用同一條命令續跑。已封存的研究可從 `research --run-id` 重導報告。B 使用 `message --successor` 從 A 的封存決定開立後繼，兩個日期窗的原始回應與派工分開保留。

## 材料與獨立驗證

- 酵母題只查已出版的一般食用酵母菌株資料。新版方法 `yeast-archive-lookup@2` 接受研究者明示的菌株及糖名，從同一個已登記的唯讀資料快照取數。成功的空查詢保留具名輸出和來源摘要。快照缺失則回傳工程錯誤。[兩個對照](yeast/literal-tests.log)
- 水文 A 用 2023 年 4 月 1 至 3 日的兩站日均流量，B 改用 6 月 1 至 3 日的新站點配對。每題兩站各有三筆經 USGS 核准的日均值。±25% 是本研究設定的敏感度情境。來源覆蓋限於這六筆站日觀測與指定時窗。[研究設定](hydro/study.json)
- 原始回應固定後，`hydro/materials.py` 先用每日立方英尺流量獨立計算三日總量，再轉公升。正式分析插件讀取逐日區間的公升／秒資料。模型只接收題面與輸入資料，評分用的 `reference.json` 沒有傳入 Goal。[A 的原件](hydro/a/usgs.json) · [B 的原件](hydro/b/usgs.json)
- 發行前預驗核對原件識別碼、兩站總量、共同時窗以及差值上下界。兩個插件的輸出均在預定容差內。初次預驗發現已發行方法需要 `trial` 定位欄位，在水文首筆進場前補上站號及日期並重新固定輸入摘要。[預驗結果](hydro/preflight.json) · [原始登記](hydro/preflight-initial-registration.json)

```bash
.venv/bin/python -B acceptance/harness_refactor/field/hydro/materials.py a
.venv/bin/python -B acceptance/harness_refactor/field/hydro/materials.py b
.venv/bin/python -B acceptance/harness_refactor/field/hydro/preflight.py ../harness-refactor-live/hydro/release
PYTHONPATH=gx-h:. .venv/bin/python -B -m unittest -v acceptance.harness_refactor.field.test_literal_archive
```
