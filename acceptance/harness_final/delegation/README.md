# A 場第三席的委派回執

正式 A 場的 Lead 在事件 457 獲准開席，分配 18,000 tokens 與一個 worker task，省略了 api_calls。宿主當時提供的工具宣告只要求各額度是非負整數。開席回執也沒有告知省略這個欄位等於零次子模型回合。該席未啟動模型，事件 460 保存了 GX 的 PARTIAL 報告，461 喚醒 Lead。a-original-contract.json 保存了當時的原生呼叫、實際宣告、回執鍵及原始事件。正式 A 場與結果保留。

修補只在現行開席回執遇到零次子模型配額時附上 api_calls_notice。回執區分顯式零與省略，說明 PARTIAL 的實際含義。若 Lead 仍需子席，可保留原報告，以新的 operation_key 從餘額中另開委派。工具宣告與正常有額度的回執沿用原格式。

候選由普通 gx-p0 research 建立新 Run，再以受控原生回呼採用 A 的原配額。省略、顯式零均得到唯一 PARTIAL 且沒有子模型 Root，新開席撥一次 api_calls 成功啟動。重送原開席回執一致。三個受控案例見 candidate-callbacks.json。相關五項測試通過，可從產品根目錄重跑：

```sh
PYTHONPATH=gx-h:. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest -q acceptance.harness_final.delegation.test_opening_budget acceptance.v62a.test_terminal_funding acceptance.v62a.test_module_lifecycle
```
