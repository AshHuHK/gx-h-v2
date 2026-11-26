CLOSED — V6.2.2 工程收尾

2026-11-26 UTC。歷史發行索引、原始公開 Run 重評、發行環境六組新鮮離線驗收、維護線科研正確性回歸及同路徑升級／回退均已完成。公開標準組實得 4/4，行為對照組實得 3/4。負例 F 收到 54/164，宣稱改路卻完成原比較，按預註冊規則失分。本輪使用真實 CPU、鎖定的官方 Codex App Server 與隔離網路中的本機 Responses 替身，外部模型呼叫為 0。

## 驗收版本與部署

- **H，歷史發行** `e6520f53ca19ef8eae3ec0915af74e9bfc6dcdca`。**P，該發行索引指定的產品** `a0d167cc34a51b2c687e038510372c84971616a0`。**O，舊版遷移來源** `e5b7ee977ec33854362bc2440d8f9249464aed20`。
- **B，維護起點** `5f5298f196adbba13ab8c6a5cdd8ecb857c1c05c`，取自 `v6.2-science-execution`。**M，修補後實際受測及部署的提交** `e69d71352c19583bf61e8cfeb01d409e07f98ba1`。修補分支為 `v6.2.2-engineering-closeout`。歷史發行與當時的評分依據均可按原 SHA 重播。
- 工作區 `gx-v-6` 下，`product-v622-closeout` 保存修補和本報告，`v622-closeout-deployed` 是固定在 M 的獨立安裝，`v622-closeout-20261126` 保存完整 argv、退出碼、日誌和 Run。環境使用 Python 3.11.9、鎖定 Node 22.17.0、Cordis lock 及 `codex-cli 0.169.0`，官方宿主 SHA-256 為 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`。[部署鎖與工作樹紀錄](../v622-closeout-20261126/deployment.json)

## 可重跑的驗收矩陣

表中 `$py` 為對應 checkout 的 `.venv/bin/python -B`，`$C` 為已部署的 Clash 執行檔，`W` 表示為該組新建的倉庫外輸出位置。每列連結保存實跑參數與完整路徑。除表內明列的首次失敗及預期拒絕，最終必跑命令退出碼均為 0。各套件有重疊，測試數按各自分母報告。

| 組別與執行入口 | SHA | 本輪結果及跳過 | 命令、日誌或回執 |
|---|---|---|---|
| 歷史索引 · `$py acceptance/v622/verify.py verify --require-complete` | H 查 P | `V6.2.2=PASS`、`V6.2C_OFFLINE=PASS`、`V6.2C_PAID=BLOCKED` | [索引日誌](../v622-closeout-20261126/history/verify.log) |
| 歷史兩組 · `$py acceptance/v622/public_archive.py replay --receipts acceptance/v622/receipts/{standard,formal}`（逐組） | H 查 P | 各重評四個原 Run，4/4、3/4。分數逐位元重現。重播新增 CPU Job 為 0 | [八個原 Run 與四條歷史命令](../v622-closeout-20261126/history/summary.json) |
| 定向發現 · `$py -m unittest discover -s acceptance/v622 -p 'test_*.py' -v` | H、B、M | H 37/37。B 首輪 36/37。M 修後 38/38，均無跳過。新增一例單獨檢查現行材料時序 | [歷史](../v622-closeout-20261126/history/discover.log) · [首輪](../v622-closeout-20261126/maintenance-initial/inherited/command.json) · [最終](../v622-closeout-20261126/final/inherited/command.json) |
| 維護線遺留回歸 · `$py -m unittest -v` 加任務單指定十個模組 | B、M | 兩輪各 38/38，0 跳過。涵蓋端點時序、資料取得、版本、送達、晚到、行動及裁決 | [首輪](../v622-closeout-20261126/maintenance-initial/residual/command.json) · [最終完整命令](../v622-closeout-20261126/final/residual/command.json) |
| 行動見證 · `$py -m unittest -v` 加五個模組。來源追溯 · `$py source_export_probe.py` | M | 21/21，0 跳過。另核對正式發布、CLI 狀態、匯出及五個中英文正常 Goal | [測試模組及日誌](../v622-closeout-20261126/final/witness/command.json) · [事件、來源與 Job](../v622-closeout-20261126/final/source-export/B-audit.json) |
| 發行 repairs · `$py acceptance/v622/repairs.py --work W` | P | 37/37，0 跳過 | [啟動命令](../v622-closeout-20261126/repairs-launch.json) · [測試日誌](../v622-closeout-20261126/repairs/repairs.log) |
| 發行 funding · 隔離執行 `-m acceptance.v622.public.funding_probe` 及 `-m acceptance.v622.public.batch --suite standard --host live` | P | 6/6 控制通過。拒絕案例退出 **2**，未建批次，模型呼叫 0。斷線未決保留預留，重送未重扣 | [計量](../v622-closeout-20261126/funding/bounded-invocation.json) · [拒絕](../v622-closeout-20261126/funding/negative-invocation.json) · [金額與次數](../v622-closeout-20261126/repairs-funding-crash.json) |
| 發行 compat · `$py acceptance/v622/compat/regression.py --work W --clash "$C"` | P | 11/11 組，14 個隔離子命令成功。發現 646 項，642 通過、4 項選配 DSH 跳過、0 失敗 | [命令](../v622-closeout-20261126/compat-invocation.json) · [分組日誌及跳過名稱](../v622-closeout-20261126/compat-summary.json) |
| 發行 standard、formal · `bash acceptance/v622/public/run-public.sh W "$C" standard`／`controls`，各接 `public_archive.py pack` 與 `replay` | P | 標準 4/4，8 個成功 CPU Job、66 筆本機請求。對照 3/4，7 個成功 CPU Job、55 筆請求。兩批模擬 token 分別 2,310、1,925。各自重評的 24 份逐格檔和總評一致 | [標準命令](../v622-closeout-20261126/cohorts/standard-commands.json) · [對照命令](../v622-closeout-20261126/cohorts/formal-commands.json) · [逐格證據](../v622-closeout-20261126/cohorts/summary.json) |
| 發行 crash · 隔離執行 `$py acceptance/v622/batch_recovery.py --work W` | P | 首格結題、批次控制器尚未保存回執時 `SIGKILL`。恢復後四格均結題。首格仍為 2 個 Job、13 筆請求、455 模擬 token。三項新增量均為 0，原紀錄逐位元一致 | [命令](../v622-closeout-20261126/crash/crash-invocation.json) · [故障與恢復](../v622-closeout-20261126/crash/probe/result.json) |
| 發行同路徑遷移 · 舊版建 Run、`compat/migration.py capture / check / rollback` | O → P → O | 11 條命令全數成功。兩次冷開、兩次報告重送及備份回退後，2 個 Job、2 個主張、6 份決策、7 個已結算 turn 保留，新增 Job、turn、扣帳均為 0 | [逐條命令](../v622-closeout-20261126/migration/commands.jsonl) · [前後摘要](../v622-closeout-20261126/migration/migration-summary.json) |
| 維護線相容性 · `$py acceptance/v623/campaign.py test --work W --clash "$C"` | M | 外層退出 0。產品 80/80、繼承 repairs 38/38、相容 11/11 組。發現 646 項，642 通過、4 個選配 DSH 跳過、0 失敗。正式宿主完成一個 CPU Job，簡報送達與讀回均為 49/164 | [外層退出碼](../v622-closeout-20261126/final/campaign/command.json) · [11 組](../v622-closeout-20261126/final/campaign/work/inherited/groups.json) · [宿主實收值](../v622-closeout-20261126/final/campaign/work/endpoint-host/endpoint-host.json) |
| 維護線索引和封存重播 · `v623/validate.py verify --require-complete`、`v623/public_replay.py verify`、`v623/study/archive.py replay` | M | 三條成功。公開原始 4 Run、24 份資料包重現。既有研究 7 份原生決策、4 項數值結果可複核 | [索引](../v622-closeout-20261126/final/v623-index/command.json) · [公開](../v622-closeout-20261126/final/v623-public/command.log) · [研究](../v622-closeout-20261126/final/v623-study/command.log) |
| 維護線單層與委派 · `bash acceptance/science_execution/rehearse.sh W "$C"` | M | 兩條腳本化正式宿主鏈均通過工程判讀。P4 親取 149/164 並維持。K7 子模組取得 54/164、報告送達並被 Lead 引用，撤回原分支 | [命令](../v622-closeout-20261126/final/diagnostic/command.json) · [收件、決策、執行事件](../v622-closeout-20261126/final/diagnostic/replay/verification.json) |
| 維護線同路徑升級與回退 · `$py acceptance/v623/campaign.py upgrade --work W --clash "$C" --seed SEED` | H → M → H | 原 Run 2 個 Job、8 個原生 turn，兩次冷開與兩次重送後新增 Job、turn、扣帳均為 0，回退後科研摘要相同 | [命令](../v622-closeout-20261126/final/upgrade/command.json) · [冷恢復](../v622-closeout-20261126/final/upgrade/work/check.json) · [回退](../v622-closeout-20261126/final/upgrade/work/rollback.json) |
| 獨立部署 · `bin/gx-p0 start / start / status / stop`，兩次 start 使用同一個 run key | M | 4 次退出 0，重用同一個 Run，最終 `PAUSED`，Goal `OPEN`，0 Job、0 用量紀錄 | [部署冒煙命令與狀態](../v622-closeout-20261126/final/deployment/receipt.json) |

四個選配 DSH 跳過各有環境原因。`test_gx_run_uses_sdk_model_then_returns_to_native_decision`、`test_reported_model_mismatch_keeps_response_and_refuses_delivery` 需要 `runtime/dsh-sdk/.venv`。`test_close_cold_resume_and_observe_prior_evidence`、`test_close_while_recorded_response_is_pending` 需要配套 SDK 與 runtime wheel。[逐項原因和原始日誌摘要](../v622-closeout-20261126/compat-summary.json)

## 修補與首次失敗

1. `acceptance/v622/test_propositions.py`。首輪沿用舊時序預期，將母件上已知的儀器讀數推及座標子件。先在 P 重現舊版通過，再透過正式登記與發布核對兩版。歷史政策 `6.2.2` 的回顧性判斷仍可重播。現行 `6.2.4` 對無等價證據的子件標 `timing_unknown`。子件新測結果也不會為兄弟件補證。獨立登記、其後量測的新材料仍可取得前瞻資格。修後兩個版本案例 2/2，整套 38/38。[原始失敗、兩版命令與重新發布結果](../v622-closeout-20261126/proposition-repair/result.json)
2. `acceptance/science_execution/action_effect.py`、`acceptance/science_execution/witness.py`。後繼版本已修復行動判分。取消須對應同一 Run 的原比較、決策與節點終態。替換方法版本須有相容端點、單位、原件、具名輸出和成功 Job。明填與省略相同預設參數都算維持。事前各自登記的並行比較分開核對。臨時換名稱重做原比較仍失改路分數。[取消首敗](../v622-closeout-20261126/witness-repair/cancel-red.log) · [版本首敗](../v622-closeout-20261126/witness-repair/version-red.log) · [並行首敗](../v622-closeout-20261126/witness-repair/parallel-red.log) · [預設參數首敗](../v622-closeout-20261126/witness-repair/defaults-red.log)
3. `acceptance/science_execution/test_witness_cancel.py`、`acceptance/science_execution/test_witness_version.py`、`acceptance/v624/test_comparison_language.py`。從正常 Goal 建立、執行、判讀。覆蓋取消未完成、單位不符、並行原件、只換分支名、中英文否定句，以及宣稱改路卻維持的對照。修補迭代 29/29 通過，最終 M 另跑五模組 21/21。歷史兩批仍按各自的原評分器重現。[重試日誌](../v622-closeout-20261126/witness-repair/green-combined.log) · [並行比較證據](../v622-closeout-20261126/witness-repair/parallel-receipts.json) · [最終正常入口事件](../v622-closeout-20261126/final/source-export/B-audit.json)

## 科研判斷與交接

- **舊數字與新假設**。舊讀數 7、8 在事件 26 已暴露，門檻從 10 改為 5 的新命題在事件 35 才登記。維護線將讀數列為回顧性，正式結論是 `insufficient`。新遮罩及獨立來源各有其時序。座標案例的母件、P4、K7 分別測得 164/164、149/164、54/164。共同來源的兩份子件保留各自實測事件。來源自報 1.2 在正式報告、狀態及匯出中均為 `operator_attested`，觀測版本為空。解析確認的 1.3 為 `verified`。[正式入口回執與 Run 對照](../v622-closeout-20261126/final/source-export/B-audit.json)
- **Lead 所見與所做**。晚到結果在事件 144 使舊版簡報無法直接結題。可記錄有據的 `UNREAD_EVIDENCE`，成功讀回後才可引用。新版腳本化 P4 的送達、決策及完成為事件 71、79、90。K7 報告發布 96、釋出 97、Lead 收到 105、決策撤回 114。四格中的 B1 直接取數、B2 晚委派、B3 有據早停均得分，F 的同一個原比較仍在執行，科學分為 0。[送達與處置](../v622-closeout-20261126/final/diagnostic/replay/verification.json) · [公開對照逐格事件](../v622-closeout-20261126/cohorts/summary.json)
- **真模型與下一份研究單獨計數**。歷史 V6.2C 付費原批 0/4、原未見配對 0/2、後繼真模型 P4/K7 正式簽收 0/2。V6.2 系列仍待凍結。第三沉積歷史篩選為 0 個合格候選。新研究簽收前還需處理匯出器引用首次後續決策及資格封存固定舊篩選結果兩項問題。審閱追到這兩段程式的呼叫者均屬第三研究入口。[原版索引](../v622-closeout-20261126/history/summary.json) · [下一場研究的兩個已重現問題](../v622-closeout-20261126/maintenance-compat-initial/scope-audit.json)

要本機查看這次部署的原 Run，可在 `v622-closeout-deployed` 執行下列命令。實際建 Run、重用相同鍵及停止的完整參數在部署回執中。兩次遷移均在獨立安裝與狀態備份完成，回退使用原版程式及配對的備份。

```bash
GX_CODEX_OFFLINE=1 .venv/bin/python -B bin/gx-p0 status \
  --state ../v622-closeout-20261126/final/deployment/state \
  --run-id gxr_01m8b1cna8essx6nxw
```
