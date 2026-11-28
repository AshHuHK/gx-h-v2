# 同一筆讀數的兩個來源入口

本次 `SCRIPTED_CALLBACK` 用普通 `gx research` 建立水溫 Goal，Lead 預先凍結兩次不同採樣及 10 °C 門檻。操作員透過正式 `gx observe` 分兩次交付同一註冊來源版本的兩種不同編碼，各自保存原件、讀數和取得途徑 REST／MCP。兩份原件都進入 claim，關係為 `same_data_method_consistency`，結論維持 `tested`。第三份來自事前登記的獨立採樣、測量值同為 12.5 °C，每個原件各有引用。獨立來源的兩組對照均在現有 claim owner 被判為 `independent_acquisitions`，Lead 實際收到的下一回簡報與 PI 的科研證據投影都反映 `replicated`。

此受控因果測例覆蓋來源身份已經由取得記錄標明的情況。它向兩個接口發出的外部請求為零。不同的 transport 名稱或相同計數無法證明或否定獨立性。現有插件實際兩路的既有現場回執可對到相同 accession 和版本，例如 `yeast-trehalose-jobs.json` 記錄 `P32356/202`、`yeast-sucrose-jobs.json` 記錄 `P00724/203`；本次沒有重跑這兩個歷史網路 Job。見 `acceptance/science_capabilities/evidence/`。

`provider-sources.json` 在整合 `2078eb7` 與可攜測試修補後，由普通 Goal 的證據 owner 導出。保留兩個取得途徑及三個 artifact refs。
