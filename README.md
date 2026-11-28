# GX-H 科研 Harness

新研究的 Lead 可自行選法、委派與局部重開路線，並以具名證據喚醒後續決策。普通入口、舊 Run 相容及各類回執見[Harness 重構與研究分工](acceptance/harness_refactor/README.md)。

GX 把一個研究問題保存為 Goal。Lead 可查方法卡及插件目錄、選擇量測、建立多條 Workflow 並委派工作。GX 負責執行 Job、保存具名輸出、將證據送回 Lead，並從同一 Run 的狀態繼續下一輪。

| 內容 | 程式入口 |
|---|---|
| 研究推進與多輪決策 | [研究入口](gx-h/gx_core/p0/research.py) · [Workflow 圖](gx-h/gx_core/p0/v6_graph.py) · [Lead 決策](gx-h/gx_core/p0/v62_lead.py) |
| 方法、能力目錄與插件 | [Lead 的能力視圖](gx-h/gx_core/p0/capability_views.py) · [已登錄插件](gx-h/config/workflow_packages.json) · [方法實作](gx-h/plugins/installed/) |
| 執行、評測與證據 | [科研工作流](gx-h/workflows/) · [科研適配器](gx-h/adapters/) · [證據與報告](gx-h/gx_core/p0/research_evidence.py) |
| 例題 | [沉積結構質控](examples/deposited-structure-controls/) · [酶學留出回測](examples/bglb-retrospective/) · [從研究建立可重用方法](examples/record-method-authoring/) |

已接入的實際科研資產涵蓋文獻、天然酵母酶、已發表蛋白、肽及 Fab。各類卡的使用時機、普通 Goal 啟動命令、跨題配方和新 Job 的現場回執見[科研資產與部署](acceptance/science_assets/README.md)。

## 接上現有宿主

使用已配置的 Codex App Server，本版接口驗證版本為 0.169.0。GX 從 PATH 呼叫 `codex`，也可用 `GX_P0_CODEX` 指定現有執行檔。工作區的研究狀態由 GX 保存。宿主通訊見[stdio 客戶端](gx-h/gx_core/hostobs/app_server_client.py)。

本版使用 Python 3.11 和 Node 22.17。已備妥科研依賴的環境可直接執行研究。首次安裝可在已配置代理的環境中執行下列命令。

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix gx-h/runtime/plugin-host --ignore-scripts --no-audit --no-fund
bin/gx-p0 plugin list
```

## 開始與繼續一個研究問題

```bash
bin/gx-p0 research --state "$PWD/var/top7" --run-key top7-coverage \
  --goal-file examples/deposited-structure-controls/top7.md \
  --goal-config examples/deposited-structure-controls/top7.json
```

中斷後，沿用同一個 state 和 run-key 重開。科研插件的外部資料及工具由部署方提供。例如酵母例題使用已掛載的兩個索引、BLASTp 與 UniProt MCP，介面位置見[掛載欄位](gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1/methods/mounts.json)。

## 復驗科研行為

```bash
export PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a
python -B -m unittest discover -s acceptance/science_capabilities -p 'test_*.py'
python -B -m unittest acceptance.multi_workflow.test_lead_graph_conflict \
  acceptance.multi_workflow.test_advisory_rounds acceptance.multi_workflow.test_pcna_geometry \
  tests.test_science_capability_views tests.test_acquired_documents
```

版本中各項方法實際用在什麼研究、Lead 怎麼選以及已知缺口，另見[科研能力與插件架構審閱](https://github.com/AshHuHK/gx-h-v2/blob/e773be50b46f2328c3e0edf08350c0beddc45890/acceptance/science_capabilities/GX-H_Science_Capabilities_and_Plugin_Architecture_Review_zh-TW.md)。
