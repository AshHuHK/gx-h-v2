# Main 已路由的 PI 輸入與 Lead 關題

正式首試 Run `gxr_01m8g0bb8rcxkxprpa` 在 `362157a` 關題。晚到訊息的兩條原句可在 Main 的官方 user message 找到，事件 60、61 將它們送往當版 Goal 的 Run Lead，62 確認路由。做出決策 80 的 Lead 持有游標 52 的簡報，已核對原生 turn/start 回執及簡報 SHA。兩個 Lead 官方 rollout 都查不到對應原句或 feedback ID。事件 83 正式關題。原件是訊息內的資訊，該次沒有檔案附件。

`field-before.json` 與 `field-after.json` 對同一份現場資料庫以唯讀方式重放。以暫存 SQL view 將可見事件截在關題交易的第一筆寫入之前。原程式此時查不到晚到證據，修補程式辨認出事件 60。每次回放對原 Run 的寫入數皆為零。回執匯出字數、摘要和事件身分，科研原文留在現場。

`29d6dbd` 重用既有的過時決策處置。Main 路由至當版 Run Lead 的研究資訊或給該席的 PI 回答，在已送達簡報之後進帳時，GX 讓舊收尾重新取得上下文。後繼任務在同一 Run 收到新簡報，Lead 可採納、維持、推遲或照樣結題。GX 沿用現有 Goal 與 Job；正式處置過的輸入、一般 Main 討論、其他 Goal 和支線私件均有針對性對照。

- `before.log` 與 `after.log` 為同一組普通 research CLI 測試。主回歸在已確認簡報後及決策準備完成後兩個時點注入正式 Main 回調。舊版本均接受過時 close，候選兩次均拒收，冷重啟後交付兩條原句，並接受新 Lead 的處置與結題。受控傳輸共四項測試通過。
- `after_ack.json`、`after_prepare.json` 含測試 Run、舊與新任務、被拒決策依據、兩條真實測試回執及生效決策，每案新增 Job 數為零。
- `affected.log` 的 13 個既有關題新鮮度、證據與並發測試全數通過。

從產品根目錄重跑。原場單例複驗使用獨立的註冊與 Run，由 benchmark owner 存放其正式結果。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a GX_CODEX_OFFLINE=1 python -B -m unittest -v \
  acceptance.harness_final.late_pi.test_late_feedback \
  acceptance.v622.test_close acceptance.harness_final.close.test_questions
PYTHONPATH=gx-h python -B acceptance/harness_final/late_pi/replay_field.py <archived-long-candidate-arm>
```
