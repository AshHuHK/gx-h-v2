# 宏觀 Recipe 採納與冷恢復

由一般 `bin/gx-p0 research` 先開兩個不同 Goal。第一個 Goal 的兩個研究席實際交付並保存配方。第二個 Goal 使用自己的材料，首次展開保持唯讀，測試分別選擇明示採納及提交原生委派。兩種方式都在採納交易內登記具名邏輯依賴。測試在上游撤銷、下游尚未建立、Lead 已提交等待決定時，以 SIGKILL 終止控制進程，再用相同資料庫啟動。兩筆 JSON 均記下唯一有效喚醒、下一位 Lead 實際收到的事件與缺件原因。第三個案例在同一題以相同的 prefix 及輸入開兩條分支，查到各自的研究席和 Join。撤銷其中一條後另一條仍然等待。兩條在轉成正式 branch ID 後均回到原先的實例。

`baseline-full.log`（4b6aa93）及 `baseline-slim.log`（6ec604d）保留最初探針的兩個失敗。`adoption-0.json` 為實際委派，`adoption-1.json` 為明示採納。驗收使用受控原生回調，不計入 OAuth 科研實場。

`computation.json` 的受控 Lead 將配方步驟提交給實際 CPU Job。修改為未綁定的輸入時，該交易未建立節點或 Join；Lead 使用新問題、保持原先綁定後，一次提交同時建立計算與監看。下一個 Lead 收到本題 Job 的具名輸出。

[組合結果](macro-suite.log) 記錄 17/17 通過，包含兩階段配方與原有 Join 測例。受控回調只驗證工具、派工及恢復行為。

從部署根目錄執行

```bash
PYTHONPATH="$PWD:$PWD/gx-h:$PWD/gx-h/tests:$PWD/acceptance/v62a" \
GX_CODEX_OFFLINE=1 GX_ADOPTION_RECEIPT="$PWD/acceptance/harness_final/macro/adoption" \
.venv/bin/python -B -m unittest -v acceptance.harness_final.macro.test_adoption acceptance.harness_final.macro.test_adopt_computation \
  acceptance.harness_final.macro.test_portable_stages acceptance.harness_refactor.macro.test_macro_recipe acceptance.harness_refactor.macro.test_joins
```
