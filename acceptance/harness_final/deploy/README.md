# Dual deployment and cold re-open

The full release began at `4b6aa93` on `science/multi-workflow`. The lean release began at `6ec604d` on `integration/science-assets`. Both trees were clean at entry. `baselines.json` records the two local and remote revisions, Python versions, source-tree hashes, contract digest, role projections, package registrations and optional database hashes from each installation.

## Reproduced baselines

| Source at entry | Historical suite | 92 harness tests |
|---|---:|---:|
| Full, as found | 121 passed | 86 passed, 6 distinct methods failed |
| Lean, as found | 121 passed | 92 passed |
| Full, detached, with verified archive | — | 92 passed |
| Lean, detached, with verified archive | — | 92 passed |
| Full, detached, archive absent | — | 90 passed, 2 failed |
| Lean, detached, archive absent | — | 90 passed, 2 failed |

The first full-site harness suite reported six failing subcases and three errors. Four of those failures belonged to one parameterized method. The local installed yeast reader contained an untracked compiled `archive.cpython-311.pyc`. Its SHA-256 was `a2d007acd6eca5bfa4f0082aa5967437b5bd9d02ee9ac92ad33441311fce9952`. This changed the installed package digest and suppressed multiple intended capabilities. A detached checkout with the same four mounted SQLite files passed. The cache was quarantined by digest outside the release and the original local Goal-to-CPU-Job regression passed. The package contents and pin were retained. A second intermediate observation is marked explicitly in `baseline-tests.json`. Running Python with `-B` avoids writing compiled Python into the immutable installed provider.

The clean unmounted baselines expose a different problem. One test required the archive dependency to appear ready, and one required a successful real archive Job. Both exercised an absent licensed data mount. `baseline-tests.json` identifies these methods, links each run to its raw and published log digest and preserves distinct method counts and durations. The snapshot members are installed site data. Clean-checkout tests can exercise the genuine missing-mount path, and a provisioned site can assert every registered byte digest and execute an actual read.

## Rerun

From each release root use its own Python and checkout. The historical cohort is the exact 121 recorded test IDs.

```bash
.venv/bin/python -B acceptance/harness_final/deploy/runtime_check.py .
.venv/bin/python -B acceptance/harness_final/deploy/site_tests.py . historical
.venv/bin/python -B acceptance/harness_final/deploy/site_tests.py . harness
.venv/bin/python -B acceptance/harness_final/deploy/site_tests.py . final
.venv/bin/python -B acceptance/harness_refactor/integration/cold_upgrade.py
```

For a recent Goal, supply the starting SHA for that installation. The in-place upgrade fixture creates an ordinary Goal with an explicit empty scope, closes it, starts the candidate in a fresh process at the same release path and checks the report hash, Run ID, stored scope and zero additional work.

```bash
.venv/bin/python -B acceptance/harness_refactor/integration/cold_upgrade.py --base 4b6aa93 --expected-policy gx.harness/2
```

For the lean installation substitute `6ec604d` as the base. For an existing closed research state run `cold_reopen.py` with the original release, original state directory and Run ID. It verifies the original Job artifact bytes, provider identities, decision and report hashes, original recipe digests, absence of added work and the original release/state binding. An absent external Job on a particular prior Run stays explicit.

```bash
.venv/bin/python -B acceptance/harness_final/deploy/cold_reopen.py . /absolute/path/to/original/state gxr_...
```

A separate read-only check opens an earlier field archive's genuine saved macro recipe and re-evaluates the original successor's recorded adoption arguments against the closed state. The baseline and candidate projections must have the same card, body and expansion SHA. This check performs zero writes. The original native model work remains attributed to its original installation.

```bash
.venv/bin/python -B acceptance/harness_final/deploy/archive_recipe.py . /absolute/path/to/hydro/state daily-gauge-volume-review@1
```

## 實際發行與現場釘選

| 用途 | 完整版 | 精簡版 |
|---|---|---|
| 原場研究的凍結產品 | `362157ae` | `e9e41838` |
| 修復舊 Goal 檢索回歸後的發行產品 | `fd1aa75c` | `8759016c` |

首次整合保留兩條紅色紀錄。兩站的 121 項歷史回歸均在舊 Goal 上失去「文獻」查詢第一頁的 `scholarly-fetch@1`，其餘 120 項通過。修復後舊合同沿用原有排序。精簡站的新證據測項首次因缺少既有來源測試夾具而無法載入，補入完整版既有的同一個檔案後，精簡站可獨立走完官方回調測例。現場科研的凍結版本與後續修正版分列。

加入 adapters 後，兩版在舊 Goal 檢索修復階段自原產品起點共修改 51 個交付檔案。50 個共通檔案的 Git blob 完全相同，另 1 個為站點插件登錄表。精簡站保有唯一的原有 record-coverage 登錄及相同摘要。兩個產品間原先不同的 11 個路徑均保留，包括兩個不同的 CLI 部署入口。發行站逐一從安裝目錄計算 32／33 個插件的位元組摘要，分別驗證 4 組模型與 effort 角色投影及 4 份授權資料庫檔。原資料庫 schema 摘要保持一致，兩版目前合同內容摘要均為 `c22a981602c3fe6a8e0972fc18382a0353e58132bb49da5fd1e01b18c6fe15a2`。

| 歷史科學 Run | 核對後的在庫工作 | 冷開新增 |
|---|---|---|
| 完整版 `gxr_01m8fq45p3rfgk2cv5` | /2，514 個 Run 事件，3 個外部 Job、11 份具名原件逐檔核對。 | 0 個 Job、0 個模型回合 |
| 精簡版 `gxr_01m8frjb04ng1edrvg` | /2，32 個 Run 事件、原場沒有外部 Job。 | 0 個 Job、0 個模型回合 |
| 精簡版 `gxr_01m8fekb5gbjkvze1w` | 舊版未標記政策，74 個 Run 事件，1 個外部 Job、3 份具名原件。 | 0 個 Job、0 個模型回合 |

另外在每站固定路徑上分別以歷史無標記版本、本站 /2 版本建立並封存有顯式空 scope 的 Goal，再原地升級。四案均保留原 Run、25 個事件及同一報告 SHA，新增工作為零。完整版與精簡版各有一次獨立啟動 CLI 的回執，使用原生研究入口和極短時限檢查安裝。兩次都已開出新 Run，回傳 75 表示到時退出。這兩項是啟動測試。

原水文研究的 `daily-gauge-volume-review@1` 內容摘要為 `9e42621326774cb2b0eebac0c4144aef597065b8ec204e3a770248341ce2188e`。兩個舊版與兩個新站以唯讀方式重播後繼題記錄的原始採納要求，recipe 卡、來源綁定與展開回覆逐位元一致。新舊版本都辨認出已存在的 `computed` 節點，沒有寫入舊實驗。

| 最終測試組 | 完整版 | 精簡版 |
|---|---:|---:|
| 掛載站歷史相容 | 121/121 | 121/121 |
| 掛載站既有 Harness | 92/92 | 92/92 |
| 掛載站本輪新測（含前版冷開） | 55/55 | 55/55 |
| 獨立乾淨檢出、確實缺少授權掛載、既有 Harness | 90 通過、2 明示跳過 | 90 通過、2 明示跳過 |
| 同一乾淨檢出、本輪新測 | 55/55 | 55/55 |

逐案計數、耗時及 SHA 收錄於 [tests.json](receipts/tests.json)。[兩站差異逐檔對照](receipts/release-parity.json) · [現場產品原凍結對照](receipts/formal-parity.json) · [歷史水文配方](receipts/baseF-recipe.json) · [雙語保留集](../holdout/README.md)

## 晚到 PI 原件的修後版本

正式場首試發現 Main 已把 PI 的補充資料路由入庫，舊的 Lead 決策包仍可結題。本組修後產品的完整／精簡凍結為 `6077e26c`／`ccd0513e`。在這兩站分別驗證補充原件可在下一個有效 Lead 決策包讀到，舊包結題要先更新，私有分支和已處置的資料維持既有作用域。修後的原場資料與首試分開保存。

計入 adapters 後，修後產品共有 52 個相對起點變更的交付檔，其中 51 個共通檔的 blob 在兩站相同，登錄表保留各站插件。兩個部署的歷史相容 121/121、新版 63/63、晚到回饋及受影響定向 13/13。撤去全部站點數據的乾淨檢出亦各通過新版 63/63。先前的中文、英文方法保留集在兩站均為 4/4，含雙語成對命中 4/4、僅供參考及零結果各 1/1。原 full 與 lean /2 真 Run 冷重開仍增加 0 Job、0 原生回合。

完整版的既有 92 項套件首次有一個測例在 2 秒時讀到仍在 RUNNING 的 CPU 節點。修正後的測具反覆用正常入口恢復相同 Run，當指定節點到達終態再核對原 Job ID 與授權，總耗時以 20 秒為限。兩個部署的既有套件最終均為 92/92，首次輸出及修後觀察均已保存。[修後逐站回執](receipts/followup-tests.json) · [修後產品檔與登錄差異](receipts/followup-parity.json)

## 書目引用的最終產品

完整版 `e8140c6c` 與精簡版 `569657ae` 包含限定具名 JATS 原件、作者或引用鍵的書目核對。各自安裝的 `gx-h/adapters` tree 是 `bd5cce6eb84b4336d562e026449d78e9210628bb` 與 `5400b8b75f38d7407e7e0594464cbc199b6f32d0`。精簡站的科學解析器採三方合併並比對原始 bytes。唯一的解析器差異是本站的 PDF 依賴提示指向 `requirements.txt`。

這次把完整的 adapters 路徑納入逐檔產品比較，對原凍結、修正排序、晚到 PI、書目修正四個世代分別補記摘要，保留先前命名範圍較窄的原有文件。最終兩站對原起點共有 54 個變更的交付檔，其中 52 個 blob 完全相同，另兩個為站點插件登錄表、科學文件解析器。新檢查也驗證其餘原有跨站差異沒有擴大。[四世代原場回溯對照](receipts/formal-parity-including-adapters.json) · [排序修正版](receipts/release-parity-including-adapters.json) · [晚到 PI 修復版](receipts/followup-parity-including-adapters.json) · [最終逐檔差異](receipts/bibliography-parity-including-adapters.json)

書目最終版的兩個部署分別通過歷史 121/121、既有 Harness 92/92、新增套件 67/67，書目、既有科學讀數及真資料庫邊界定向 16/16。乾淨無掛載的兩個檢出分別為新版 67/67、既有套件 90 通過及 2 個要求授權資料的明示跳過。精簡站一開始缺少舊版已選數值行測具，加入完整版同一檔案後重新實跑。原題首次把書目卷號與年份視為需要科學量測的數字而拒絕提交，該拒絕與事後重播分別保存。最終站點的舊 Run 冷重開均為 0 個額外 Job、0 個原生回合。[最終測試與首輪缺少測具的紀錄](receipts/bibliography-tests.json)
