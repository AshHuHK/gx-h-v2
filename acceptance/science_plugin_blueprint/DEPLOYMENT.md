# 科研插件候選的部署、升級與回退

本次使用完整研發版 `science/multi-workflow@4759c260` 和精簡版 `release/science-capabilities@9eec8fd` 的產品目錄。精簡版與 `release/science-harness@1525245` 的基準 Git tree 相同。部署時保留精簡版本身已有的 record-coverage 方法、Python 安裝指引及其他原有檔案。驗收分兩步。先在單獨的精簡目錄准入一個普通 Goal，再以同一 Run ID 恢復它。真模型科研表現請依各題的 Run、Job、產物和決策回執判讀。

## 開始和繼續

在欲使用的精簡候選目錄執行下列命令。候選的 Python 3.11 環境已安裝發行版鎖定的 Biopython、NumPy 和 pypdf；Cordis 使用該版的 Node 依賴。要走線上科研，請使用既有官方 Codex OAuth 登入和工作區 Clash。從精簡候選執行 `plugin verify` 會載入該目錄聲明的插件及依賴並列出摘要。

```bash
source .venv/bin/activate
bin/gx-p0 plugin verify org.gx.publication-discovery@1.0.0 org.gx.archival-fold-calibration@1.0.0

bin/gx-p0 research --state /path/to/new-project --run-key observations \
  --goal-file /path/to/question.md --goal-config /path/to/materials-and-scope.json
bin/gx-p0 research --state /path/to/new-project --run-id RUN_ID
```

新研究使用 `research` 入口。在 Goal 設定中登記材料與方法集合；已有明確能力範圍可照舊給定。模型從目錄找出方法，在已凍結的範圍內啟用。範圍之外的候選附適用性資訊，後續是否需要改變範圍由科研需求決定。查目錄本身不消耗 GPU 作業。

## 舊 Goal 的升級與回退

原生狀態綁定部署的完整來源路徑。現有專案留在它所屬的部署入口。要原地升級，先停止該專案的 Driver，確認寫入完成，保存原本的程式版本和完整狀態目錄（包括資料庫、其 sidecar、產物及 native state marker）。把候選安裝到原部署路徑，再對同一狀態與 Run ID 執行 `research`。隔離候選可直接使用全新的狀態目錄。跨部署複製現有狀態時仍須遵循現有路徑所有權規則。

驗收使用 `4759c260` 真實舊程式准入三個舊 Goal，分別為 deep 空範圍、deep 已有 pin、quick 空範圍。三者都已綁定 Lead，原地換上新程式後，重開同一 Run，檢查舊契約、研究簡報、輸入、能力和 native 查詢介面，最後由原 Lead 完成一次決策。持有舊 pin 的 Goal 還啟用了原本允許的方法。舊 quick 的 native 工具清單保持原樣。這項測試使用受控 native transport，模型推理次數為零。

若升級後要退回舊版，保留新版已產生的 Run、Job、產物及外部計算憑證。新研究可以從先前部署開新專案。需要恢復升級前的既有 Goal 時，停妥 Driver，從同一時間點的舊程式及狀態快照還原到原路徑。確認升級後提交的外部 Job 狀態再繼續；其產物仍可供研究員另行引用。

## 重跑驗證

從完整研發 checkout（需包含 `4759c260` 的 Git 物件）執行。指定新的輸出路徑，原始失敗及前次回執會保留。

```bash
.venv/bin/python -B acceptance/science_plugin_blueprint/legacy_upgrade.py --output /tmp/gx-legacy-NEW.json
```

從精簡候選執行本次變更涉及的離線驗證。

```bash
PYTHONPATH=.:gx-h:gx-h/tests GX_CODEX_OFFLINE=1 .venv/bin/python -B -m unittest \
  acceptance.science_plugin_blueprint.test_catalog \
  acceptance.science_plugin_blueprint.test_catalog_survey acceptance.science_plugin_blueprint.test_catalog_survey_sources \
  acceptance.science_plugin_blueprint.test_scholarly_pages \
  acceptance.science_plugin_blueprint.test_reading_skill acceptance.v624.test_cheap_job \
  acceptance.science_plugin_blueprint.test_method_health \
  tests.test_v62_module_funding tests.test_publication_discovery \
  tests.test_science_capability_collections tests.test_researcher_handoff \
  acceptance.multi_workflow.test_multiturn_method_survey tests.test_v6_named_ports \
  acceptance.science_plugin_blueprint.test_recipe_binding.RecipeProviders \
  acceptance.science_plugin_blueprint.a1.test_hydrometry
```

精簡包附有已封存的公開 Top7 校準題和兩份獨立 CPU 水文插件示例。Top7 題使用 `acceptance/science_plugin_blueprint/b0/question.md` 與同目錄 `goal.json`，並由具備既有 AF3 參數掛載和 GPU 的執行環境承接 Job。模型程式、參數掛載和外部服務健康度依部署實況；模型或環境故障應保留在 Job 記錄。兩個水文示例位於 `examples/plugins/hydro-concurrent` 和 `examples/plugins/hydro-marginal`。受控正反盲題請分開建立安裝目錄，分別安裝對應的示例，再啟動 `a1/positive` 或 `a1/negative` 下題目和設定。
