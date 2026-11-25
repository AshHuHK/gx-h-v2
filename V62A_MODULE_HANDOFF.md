# V6.2A 與 V6.2B 的整合介面

A 的工作樹為 `gx-v-6/product-v6.2a`，分支為 `v6.2a-local-modules`，起點為 V6.1 發行提交 `c4c5b66a55739b38b34684360810d05f18f9a21a`。開發與驗收使用本地公開材料、獨立 state、暫存目錄及 loopback。三個單層前置修復在本機獨立分支 `v6.2a-prerequisites` 的 `c6fc20acdf29f3760c2764bbc80e88c96241e893`，可固定為單層程式對照。B 可以在自己的分支編寫插件，待 A 的固定提交通過後再執行整合測試。

## 交給插件作者

在包的 manifest 宣告 `contract: research_module` 的 extension，並在 `register(ctx)` 中呼叫 `ctx.research_modules.register(...)`；一個套件可註冊多個 `module_id@version`。註冊包含可用科研 effort、用途、預期產物、成本級別、焦點、工具、方法及輸入角色。GX 在 Run 啟動時固定宣告、方法與套件摘要。B 的獨立插件可以直接沿用此 API。[本地盤點插件](gx-h/plugins/installed/org.gx.data-inventory/1.0.0/plugin.py) · [範例授權](examples/v62a-data-inventory/grant.json)

Goal 的 `research_effort` 獨立於模型推理 effort。預設 quick 沿用單層入口。medium 與 deep 可用 `research_modules` 指定可選模組，用 `module_policy` 限制並行數、輪次與簡報容量。Lead 依目錄實際選擇。簡報中的方法過多時，`gx_query_catalog` 支援按類別、查詢及分頁取得授權範圍內的候選，按確切 reference 查詳細規格。資料盤點沿用兩個已登記的 CPU 方法；2RE9 的專用質量工具只用於該份輸入。

## 交給 Lead 與模組

| 入口 | 誰呼叫 | 內容 |
|---|---|---|
| `gx_open_module` | Run Lead | 給出 `module_id`、`module_version`、`focus`、`expected_delivery`、`round`、`input_refs`、`capabilities`、`sub_budget` 與 `operation_key`。可選 `wake_on` 為 counterevidence、gap 或 partial。 |
| `gx_decide_revision` | 持有當次任務及租約的 Lead 或模組 | 模組用凍結簡報的 `branch_id` 增加或取消自己的節點；圖修訂 CAS 由原 owner 執行，Job 結果由 JobService 留存。模組實例可在另一個 thread／租約接回工作。 |
| `gx_submit_module_report` | 模組 | 回交狀態、發現及逐項來源、缺口、產物、建議與操作鍵。GX 根據已完成工具或 Job 的收據驗證依據，固定作者、版本與全文 artifact。 |
| `gx_cancel_module` | Run Lead | 撤回特定實例；已完成的作業保留原身份與實際費用。 |

以 `module_instance_id` 在多輪追蹤同一次委派，以 `scope` 和 `budget_ref` 關聯收費。工作者只持有本次任務租約。`v62.module.wake` 指向已對 Lead 釋出的報告，載有 round、module instance IDs、report refs 和喚醒原因。後續模組使用相同報告語義；B 的科學評分可按該 Lead 實際收到的凍結簡報、當次引用及下游動作建立獨立紀錄。[正式原生工具合同](gx-h/contracts/mcp_tools.json)

發起委派的 Lead 提交當次決策時，GX 固定該輪成員。即使第一個模組在 Lead 委派第二個模組之前已經完成，兩份報告仍可同輪送達。顯式監看命中可提前送達。已結束的輪次不能追加成員；Lead 可開下一輪。

B 評估「Lead 當時讀到了什麼」時，使用該租約的 `p0_roots.brief` 或對應的 `tasks.input_json.brief`。超限時 GX 交付壓縮簡報與 `full_brief_ref`。只有存在對應的工具回執，才能把經由引用追讀的內容算作當輪已讀。完整研究快照另存於 `research_contexts.snapshot_json`，供追溯及核對來源。
