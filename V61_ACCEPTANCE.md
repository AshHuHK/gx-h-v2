# GX-H V6.1 驗收

**結構放行 PASS。新付費模型因證據改圖 NOT RUN。** 三項科研試驗的分母分別是一個兩沉積研究、25 次盲測決策、三個各一 Run 的比較臂。歷史付費紀錄另行保存。使用方式、固定依賴、備份與回退見[部署說明](V61_DEPLOYMENT.md)。

## 原始碼與提交

| 版本階段 | Git SHA |
|---|---|
| V6 審查基底 | `47fc728d32bc10ba478df6a063529a8d98f98828` |
| 第一份獨立可審的合同與遷移實作 | `c625a6d0904d6ccb3d5f34c6b10d33a902f57377` |
| CORE_READY，含舊靜態庫與並行 Reactor 修復 | `4422931fea3bf4f776e5d1820304c83564ae98d2` |
| PLUGIN_ONLY，新增 CSV 插件及其獨立驗收 | `3f51a539c6baa21b0132d92a81600d2c612c4572` |
| 正式驗收候選 | `48af72c3861299350047dd3bae25acfe6a0bc1bd` |

基底之前的正式工程及 live 凍結點為 `fe7ff695a7328c198ffb5bb90b50cc2391f6f397`，歷史參照為 `e02df99757686982744813e5de745e86ef12e0a3`。CSV 提交包含 11 個檔案。針對 `gx-h/gx_core` 及 `gx-h/contracts` 的完整樹比對退出碼為零。[逐階段提交與檔案清單](acceptance/v61/verification/revisions.json)

## 放行門檻

| 門檻 | 判定 | 實測與可核對的證據 |
|---|---|---|
| A　新庫、舊庫與在途恢復 | PASS | 舊 V6 的真實 OAuth 研究兩次冷恢復後，圖、主張、5 個 Job、產物身份和 259,718 個已確認 token 一致。V6 程序在 JobService 受理後中止，V6.1 接回同一 Job；第二次恢復新增提交及扣費均為零。舊靜態 Run 用原 recipe 和 Job 繼續至下一個研究任務。原 state 回退和原版二次讀取亦有收據。[升級和故障注入](acceptance/v61/migration-release/comparison.json) · [靜態 Run](acceptance/v61/runmode/static-upgrade.json) · [合同與回退 ADR](gx-h/docs/v6.1-contract.md) |
| B　相同 Goal 入口擴展兩個領域 | PASS | 新 Goal 預設可用方法為空。明示能力解析到固定 provider 版本與摘要，跨 Run、缺提供者、過期授權及工具越權均有派發前拒絕測試。新蛋白包完成 4 個真實 Job 並獨立重算 6 個配適；舊 pin 可解釋原紀錄與恢復待辦。CSV 由公開 CLI 安裝，官方 Codex 宿主用本機腳本驅動兩個新 thread，一個真實 CPU Job 測得兩時段平均誤差 0.000 與 0.850 mV，次輪引用結果後結案，冷開時新增請求和 Job 均為零。[授權測試](acceptance/v61/providers/receipt.json) · [蛋白測試](acceptance/v61/protein/receipt.json) · [CSV 原始收據](acceptance/v61/verification/runs/candidate-48af72c38-01-v61/summary.json) |
| C　動態與靜態分派 | PASS | mode 工廠依持久化 Run 分派。原七個通用模組的 24 處 Goal 標記與核心 grid 特判均已移除，8 個靜態工作流保持原 digest。公開 trace 涵蓋空圖、等待、重送、完成與重開。測試核對同 call 冪等、陳舊 head 被拒與提交後 outbox 派發。[工作流相容性](acceptance/v61/runmode/grid-compatibility.json) · [V6 工程收據](acceptance/v61/verification/runs/candidate-48af72c38-01-v6/summary.json) |
| D　既有回歸 | PASS | V6 原有單測 45/45，受影響舊測 62/62（含全部 3 個 geometry；與同機基底的測試名稱逐一匹配），原有工程案例 13/13，新增工程案例 7/7。REG01 的 16 個選定項目全部通過。內部 Python 543 項，539 通過、4 項因可選 DSH SDK／runtime 未安裝而跳過；P0 129/129，Node 2/2。[公開 REG01](acceptance/v61/verification/runs/candidate-48af72c38-01-reg/summary.json) |
| E　可複驗 | PASS | 基線、初版失敗、修復後候選以及各組 report/verify 均可按版本追溯。Linux 發行包於無網路環境驗證。[完整驗收檔案](acceptance/v61/verification/) · [V5 A01–A22 各案範圍](acceptance/v61/V5_SCOPE.md) |

V6.1 合同摘要為 `69d033774654803f4854f956ae3e2516bfd845a250d95bc3d6fdafb283df0dd7`，DDL 為 `39ef2566a4155d9d395d806ea556bde12a785606cddb2f6866ac528577b6bbd2`。歷史缺少 provider 的 40 個工具、14 個 bundle validator、17 個 check 已按名稱比對，本版未增加缺口。通用的 decide／stage／delegate 均有實際原生處理器。幾何重評由蛋白授權提供。原蛋白摘要 `ac5b5d5cf96e942bfc57dc8492aa56b77ad9ecee0716735a3e6338463630fce9`，新蛋白 `f0a8dac2f226aea8a69e19c39e9070f0ac2c16c76507b26f9bb92848369fa5bd`，CSV `e7bc883f9063053b6462d165460806e0c5ce1b8f52f340459fb2fedbd02df0a2`。[逐項基底及 registry 清單](acceptance/v61/baseline/base.json) · [CSV pin](examples/v61-sensor-calibration/package-pin.json)

第一個候選 `063fdbf8096a037cc8679204b573973696174668` 雖通過 7 個 V6.1 新案例，仍由原套件找出兩處真實回歸。其一是舊靜態庫的 operator 入口未經完整 store 遷移就驗證新表；其二是並行 Reactor 變更既有 pin 核對的呼叫介面。這兩處也影響該輪 REG01 的套件匯總與歷史狀態恢復。初次 61/62 舊測試、12/13 工程案例的紀錄保留。[初次候選失敗](acceptance/v61/verification/first-candidate/)

## 科研驗收

| 試驗 | 本版執行與範圍 |
|---|---|
| 兩沉積研究 | 事前固定 2RE9 和 2O0O。兩者來自同一研究；此測量只支持各自已沉積座標範圍。六次 CPU 數值預演、18 次獨立配適，最大差異為 1.55×10⁻¹⁵ Å 與 8.53×10⁻¹⁴°。官方 loopback 使用三個新 thread，首輪一個 Job，讀到覆蓋觀測後產生第二版圖並實際新增一個 Job；第三版結案。該次從 2RE9 的 49/164 位點覆蓋改派 2O0O 的第二個 Job，獲得 152/153；兩次使用不同原子選取，能驗證改圖與派工，完整的跨選取判斷須看另行執行的六次數值預演。真實模型主要實驗為 `NOT RUN`，登記 1 個研究，執行 0 個。[資料來源、配置與命令](acceptance/v61/adaptive/STUDY.md) · [最終候選逐次改圖、工作與獨立核對](acceptance/v61/adaptive-final/receipt.json) |
| 匹配盲測 | 固定三個公開、兩個保留案例，每案五次。離線預演建立 25 個獨立世界及 25 個受保護 Job。五個正式宿主回環窗口的工程檢查通過；固定回覆在四個預期需修訂的案例未達科學判準，在中性案例通過。付費決策為 `NOT RUN`，每案 0/5，共 0/25；實際模型通過率尚無觀測值。[預註冊、成本與補跑](acceptance/v61/adaptive/controls.md) · [逐案結果](acceptance/v61/adaptive/controls_receipts/offline.json) |
| 靜態 / 單 Lead / GX 多分支 | 在相同配置與資料下各執行 6 個真實 CPU Job，三臂各有 9/9 項結論符合事前判準；本機腳本驅動的兩個 Lead 各有 2/4 次刺激引發有效改圖。多分支實際有 2 個分支、0 次委派、0 次跨分支物理重疊。靜態臂使用等條件雙沉積 recipe，另以原 `tl1a_geometry@3` 單沉積 recipe 跑過 6 個 Job 並重播。正式比較為 A 靜態臂 `PASS`（1/1 Run，6/6 Job，9/9 判斷，實測 0.484 CPU 秒，模型呼叫 0）；B 與 C 兩個付費模型臂為 `NOT RUN`，各 0/1，原登記名額保留。[正式比較及續跑命令](acceptance/v61/comparison-live/README.md) · [三臂回環比較](acceptance/v61/adaptive/comparison/README.md) |

V6 歷史第一次中性對照多派兩個 Job，原始科學判定失敗。複試的三案符合產品判準，原包 verifier 仍會拒絕同 turn 新產生的引用。兩次結果與兩版檢查器均按原樣列示。[歷史逐案重播](acceptance/v61/adaptive/controls_receipts/historical.json)

原生 OAuth 登入及模型目錄已在正式宿主查證。本次未收到跨 Run 新付費額度確認，已執行的 V6.1 推理計費為 0。付費預估取自歷史實報：主要研究約 26–59 萬 token，盲測 25 次約 130–160 萬，單 Lead 與 GX 多分支各約 26–59 萬；靜態比較臂零次推理。單次已准入呼叫可超出預留，美元實付需以供應商帳單核對。[部署的實際帳戶類型與模型檢查](acceptance/v61/adaptive/study-evidence/oauth-readiness.json)

## 複驗

下列命令在原始碼根目錄執行。`GX_RUNTIME` 指向具固定 binary 的部署快取，`GX_CLASH` 指向本機 Clash 可執行檔，`GX_BASE` 指向固定 V6 checkout。使用全新目錄可保留本次與原始收據。若需使用本機歷史資源，精確參數記於各 suite 的 `commands.json` 與 `environment.json`。

```bash
source .venv/bin/activate
(cd gx-h && python -B -m unittest discover -s tests -p 'test_v6_*.py' -v)
(cd gx-h && python -B -m unittest -v tests.test_phase2a_engine tests.test_phase2a_resume_policy \
  tests.test_p0_wait_leases tests.test_native_crash_receipts tests.test_p0_roots \
  tests.test_p0_root_delivery tests.test_v3_plugins tests.test_tl1a_geometry)
evidence=$(mktemp -d)
common=(--cache "$GX_RUNTIME" --clash "$GX_CLASH" --python "$PWD/.venv/bin/python" --runs-dir "$evidence")
bin/gx-acceptance run v6-engineering "${common[@]}" --run-id v6-check
bin/gx-acceptance run v61-engineering "${common[@]}" --upgrade-baseline "$GX_BASE" --run-id v61-check
bin/gx-acceptance run all --case REG01 "${common[@]}" --run-id regression
for id in v6-check v61-check regression; do
  bin/gx-acceptance report "$id" --json --runs-dir "$evidence"
  bin/gx-acceptance verify "$id" --runs-dir "$evidence"
done
git diff --exit-code 4422931fea3bf4f776e5d1820304c83564ae98d2 3f51a539c6baa21b0132d92a81600d2c612c4572 -- gx-h/gx_core gx-h/contracts
```

密封收據可在下載的倉庫中重新驗證，如下所示。

```bash
bin/gx-acceptance verify candidate-48af72c38-01-v61 --runs-dir acceptance/v61/verification/runs
```

本機亦驗證了 Linux x86-64 可攜包。獨立程序無法讀取作者 checkout、無外網；在解包後重建固定 assets 和 1,430 份官方 Schema，安裝附帶 wheels、Cordis 和獨立 CSV 插件，執行啟動／暫停／冷恢復及 CPU 計算。工程候選的 TAR 共 297,451,520 bytes，SHA-256 為 `87f3c3c85013a1384e3d5ff5077f7f89b5fd2ecb27cf643b52bda4e95e0731ab`。另以本報告及原始收據製成正式發行 TAR；相鄰的 `.sha256` 為其摘要。[工程候選乾淨安裝與重建](acceptance/v61/verification/portable-release.json) · [原始安裝記錄](acceptance/v61/verification/portable-records.tar.gz)

已交付環回推理的完整請求、可稽核決策、實際工作及使用量。原 OAuth 工作空間由操作員私下保管。M3 的成熟度裁決、封存、監看、批評者與 PI 簽核，以及後續多供應者與文獻整合，維持計畫中的後續階段。
