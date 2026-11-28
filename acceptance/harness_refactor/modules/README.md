# 原件取證、臨時研究員與實例內方法選擇

普通 `gx research` 把本次研究契約固定在 Goal。quick、medium、deep 的 Lead 都可用 `gx_read_source` 領取原件的段落定位、原文及回執。quick 預設一個並行研究員名額。PI 明列的可用模組、可用方法、資源和並行上限仍會生效。

Lead 透過 `gx_open_module` 委派已登記的模板，或用 `ad_hoc@1` 交給為本題組成的研究員。Lead 指定問題、交付、輸入、預算和 operation_key，可另填 researcher、route_intent 及 method_choices。PI、站點和具名模板的交集界定實例能選擇的方法。communication 為 isolated 的委派保留給盲評。實例用 `gx_query_catalog` 查方法，以 `gx_select_capability` 選定版本；選擇回執帶局部修訂號。每個新 Job 綁定該分支的 provider 與方法摘要。其他實例與 Run 的方法表各自獨立。

新契約將 round 當作顯示編號。Lead 可於另一輪展開後回到先前的編號追加工作。GX 在報告交易內以實例 ID 發送一次交付事件，無須等待兄弟研究員完成。明示的 max_rounds 上限仍有效。既有 Run 可從凍結契約辨識，沿用原有目錄、工具及聯合釋出邏輯。

## 驗證範圍

- `baseline.log` 在 50d31f5 執行同組普通入口測試，記錄 medium 主題限制、工具授權和臨時委派的原始錯誤。`first.log` 保留最早一次使用錯誤入口的測試夾具結果。
- `verified.log` 涵蓋新契約，以及原有目錄與報告發布的相容測試。測試透過普通 Goal 啟動，原生工具回調由測試控制。
- `local-selection.json` 記錄兩位研究員的分支 ID、一筆實際 CPU Job、provider 版本、輸出及新進程接回的結果。這份回執的判斷由測試驅動。真模型研究另以現場 Gate 的 session 與研究資料庫交付。

從完整產品目錄重跑：

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v \
  acceptance.harness_refactor.modules.test_autonomy \
  acceptance.harness_refactor.modules.test_local_selection \
  tests.test_v62_module_catalog tests.test_v62_modules_publication
```
