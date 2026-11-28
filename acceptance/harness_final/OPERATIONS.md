# 部署、啟動與恢復

在選定的完整站或精簡站根目錄執行。兩站各自使用原本的 Python 環境、站點配置與插件註冊。新建普通科研 Goal 時，系統會把當次控制合同記為 `gx.harness/3`。現有 Goal 從其固定的合同恢復。

```bash
bin/gx-p0 research --state "$STUDY_STATE" --run-key "$STUDY_KEY" \
  --goal-file "$QUESTION_FILE" --goal-config "$MATERIALS_AND_LIMITS" --research-effort deep
bin/gx-p0 research --state "$STUDY_STATE" --run-id "$RUN_ID"
```

`--goal-file` 是問題原文，`--goal-config` 指向本站的具名輸入與資源配置。新題的 Lead 在首次目錄總覽中引用 PI 原句，記錄全程條件、階段順序與偏好。已有顯式 scope 的舊題沿用其固定範圍。控制器中斷後以相同的 state 和 Run ID 接回；有在途 Job 時保留原有提供者句柄。新建 state 或更換 Run key 會建立另一份研究。

以下命令僅使用測試當地的狀態與資料。第一項涵蓋本輪新增工程案例，第二項覆蓋前一輪既有行為。

```bash
.venv/bin/python -B acceptance/harness_final/regressions.py
.venv/bin/python -B acceptance/harness_refactor/integration/regressions.py
```

### 升級與回退

原地升級保留 release 路徑、state 路徑及原站的掛載配置。先停止該 state 的 Driver；把 state、產物目錄、站點配置及其匹配的發行版本一起留存。在新二進位的相同位置執行上面的 `--run-id` 命令，對照原 Run、報告摘要與新增 Job 數。測試用的兩站歷史提交為完整版 `4b6aa93`、精簡版 `6ec604d`。

需要回到舊版時，將新站工作另存，恢復同一份升級前的程式、配置及狀態。若作業在回退點之後已被供應端接單，先憑該 Job 的外部句柄核對狀態，再恢復會派工的 Driver。新合同產生的 Run 可在它的候選部署繼續。現場研究保留首試的 Run 與回執。

食品蛋白站的合法權重僅配置在該站；另兩場檔案庫研究及三臂比較各有獨立工作目錄。個別案例的題目、露出時序及執行指令見 [GPU 現場](gpu/README.md) 和[三臂比較](benchmark/README.md)。
