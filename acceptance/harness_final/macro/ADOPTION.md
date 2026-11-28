# 宏觀 Recipe 採納與冷恢復

由一般 `bin/gx-p0 research` 先開兩個不同 Goal。第一個 Goal 的兩個研究席實際交付並保存配方。第二個 Goal 使用自己的材料，首次展開保持唯讀，測試分別選擇明示採納及提交原生委派。兩種方式都在採納交易內登記具名邏輯依賴。測試在上游撤銷、下游尚未建立、Lead 已提交等待決定時，以 SIGKILL 終止控制進程，再用相同資料庫啟動。兩筆 JSON 均記下唯一有效喚醒、下一位 Lead 實際收到的事件與缺件原因。

`baseline-full.log`（4b6aa93）及 `baseline-slim.log`（6ec604d）保留最初探針的兩個失敗。`adoption-0.json` 為實際委派，`adoption-1.json` 為明示採納。驗收使用受控原生回調，不計入 OAuth 科研實場。

從部署根目錄執行

```bash
PYTHONPATH="$PWD:$PWD/gx-h:$PWD/gx-h/tests:$PWD/acceptance/v62a" \
GX_CODEX_OFFLINE=1 GX_ADOPTION_RECEIPT="$PWD/acceptance/harness_final/macro/adoption" \
.venv/bin/python -B -m unittest -v acceptance.harness_final.macro.test_adoption \
  acceptance.harness_refactor.macro.test_macro_recipe acceptance.harness_refactor.macro.test_joins
```
