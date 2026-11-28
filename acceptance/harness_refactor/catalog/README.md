# 普通 Goal 的方法目錄與首次調查

新建普通研究 Goal 在沒有顯式授權選項時，從部署端的 `gx-h/config/science_site_access.json` 讀取允許選用的提供者與方法版本。建立 Run 時保存其實際套件摘要和方法摘要，選用後才能派 Job。增加跨專案可用的方法時，先照既有安裝入口發布套件，再調整該部署的具名方法清單。已運行的 Goal 持續使用凍結時的清單。

「本題只查文獻」這類獨立、未引用的明確指令可將默認清單縮到公開文獻。顯式空範圍、禁止方法的正式 Intent 以及歷史回放維持各自原有語意。首次目錄查詢即使沒有命中，仍可繼續。新證據到達時，Lead 取得更新後的上下文並自行選擇繼續查閱或直接提交。

`capability_facts` 的四個欄位各自回答以下問題。

| 欄位 | 判定依據 |
| --- | --- |
| `reference_only` | 歷史參考卡，沒有當前執行綁定 |
| `installed` | 當前發行物已核驗宣告，提供者和方法程式仍在部署目錄 |
| `dependency_ready` | 宣告的本機執行環境、入口程式、資料和權重當前可讀或執行。未能查明寫為 null。外站服務的實際可用性由 Job 回報 |
| `executed_in_run` | 同一個 Run 中有該綁定成功的 Job 與具名結果。模組只能看自己的執行，Lead 可見已公開的來源 |

在 `50d31f5` 的獨立 checkout，以當時四個驗收案例重跑，三個重現了缺口。原始輸出見 `baseline-red.log`。整合時另從普通入口重現舊式文字方法卡阻斷首次調查，見 `legacy-card-red.log`。候選版八個專項案例，再加十六個受影響的原有回歸，見 `verified.log`。CPU Job 的具名結果、輸入與結果摘要、Run ID、Job ID 和保留狀態位置見 `cpu-receipt.json`。決策回合使用可控的原生工具回調，資料庫 Job 使用正常 CPU 工作者。安裝檔與掛載遺失在 Job 完成後注入，已產生的結果仍可追溯。

在產品根目錄執行以下命令可重驗本組。將 `GX_HARNESS_CATALOG_STATES` 指向產品以外的目錄會留下完整的 Run 狀態。

```bash
GX_CODEX_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v \
  acceptance.harness_refactor.catalog.test_open_discovery tests.test_science_capability_collections \
  acceptance.science_plugin_blueprint.test_catalog_survey acceptance.science_plugin_blueprint.test_catalog \
  acceptance.science_plugin_blueprint.test_method_health tests.test_publication_discovery
```
