# GX-H 自主科研平台 · Science Capabilities

從一個研究問題開始。Codex Lead 選擇量測、分派工作、閱讀結果並決定下一步。GX 保存多輪對話、Workflow、Job、具名產物與結題依據。這個發行分支提供 Linux x86_64 版本的程式、23 筆插件版本登錄、研究範例與驗收。

| 要做的事 | 從哪裡開始 |
|---|---|
| 在新機器安裝 | [使用本機 Codex、安裝 GX 依賴與啟動研究](RELEASE.md) |
| 開始研究或恢復同一 Run | [科研入口、Goal 與命令](acceptance/science_capabilities/README.md) |
| 了解 Lead 如何發現、選擇、調用能力 | [架構審閱、實驗回執及已知缺口（繁體中文）](acceptance/science_capabilities/GX-H_Science_Capabilities_and_Plugin_Architecture_Review_zh-TW.md) |
| 檢查已登錄的版本，或安裝自建工具 | [插件清單與摘要](gx-h/config/workflow_packages.json) · [可攜方法的五個檔案與安裝命令](acceptance/science_capabilities/portable_method/README.md) |
| 找出某項科研結論的依據 | [科研驗收](acceptance/science_capabilities/ACCEPTANCE.md) · [科研回執](acceptance/science_capabilities/evidence/) |
| 查找過往工程紀錄 | [完整開發樹及歷代驗收](https://github.com/AshHuHK/gx-h-v2/tree/e773be50b46f2328c3e0edf08350c0beddc45890) |

可用淺層 clone 取得本版程式碼，再依部署說明接上本機 Codex。

```sh
git clone --depth 1 --single-branch --branch release/science-capabilities https://github.com/AshHuHK/gx-h-v2.git
```
