# 同一筆讀數的兩個來源入口

本次 `SCRIPTED_CALLBACK` 用普通 `gx research` 建立水溫 Goal，Lead 預先凍結兩次不同採樣及 10 °C 門檻。操作員透過正式 `gx observe` 分兩次交付同一註冊來源版本的兩種不同編碼，各自保存原件、讀數和取得途徑 REST／MCP。兩份原件都進入 claim，關係為 `same_data_method_consistency`，結論維持 `tested`。第三份來自事前登記的獨立採樣、測量值同為 12.5 °C，每個原件各有引用。獨立來源的兩組對照均在現有 claim owner 被判為 `independent_acquisitions`，Lead 實際收到的下一回簡報與 PI 的科研證據投影都反映 `replicated`。

此受控因果測例覆蓋來源身份已經由取得記錄標明的情況。它向兩個接口發出的外部請求為零。不同的 transport 名稱或相同計數無法證明或否定獨立性。現有插件實際兩路的既有現場回執可對到相同 accession 和版本，例如 `yeast-trehalose-jobs.json` 記錄 `P32356/202`、`yeast-sucrose-jobs.json` 記錄 `P00724/203`；本次沒有重跑這兩個歷史網路 Job。見 `acceptance/science_capabilities/evidence/`。

`provider-sources.json` 在整合 `2078eb7` 與可攜測試修補後，由普通 Goal 的證據 owner 導出。保留兩個取得途徑及三個 artifact refs。

## 正式 A 場的書目引文

Run `gxr_01m8g0a3qevpds66cg` 兩次 `gx_submit_module_report` 的原始參數，在唯讀的當場資料庫及實際授權來源上重放。基線 `6077e26` 逐字重現卷號 4 和作者年份 2014 兩個首錯。原件由 CPU Job `gxj_01m8g0ep5hjp4n0txy` 交付，文章 SHA-256 為 `63b3827c77ffba679a5093262c4a5450e07e654aff194733048517363958941f`。其 JATS front 登記第一作者 Deng、FEBS Open Bio、第 4 卷、2014 年、200–212 頁及三種文獻識別碼。兩次原始 call 的 SHA、逐欄結果和文字位置見 `bibliography-first.json` 與 `bibliography-replay.json`。

新版 /3 模組引用現有已核對 JATS 原件時，具名引文可使用該文自己的作者年份、期刊卷年頁、DOI／PMID／PMCID、具名圖表及實際檔案摘要。原件必須在該 finding 的有效來源內，相鄰書目欄位須有共同來源。書目不產生 verified metric。新舊工具的輸入／輸出規格相同，舊 Goal 的數字規則保持原版本。

第一筆的純書目 finding 已能通過，完整原始報告則在下一項碰到原文未提供為型別化別名的 `α-1,4`。JATS 表格的原件 p263、p271 是包含箭頭的完整化合物名稱。以兩段各自的原文提交，由原本的逐段引文路徑均驗證成功。未建立受控同義詞關係的化學簡寫仍無法直接替代受信原名。第二筆原報告在書目核對後，確實仍有自由文字數值 0。使用本席既有的 verbatim 抽取 p264、p272 各自提交原表格引文，兩筆均驗證成功，保留原段、來源與 quote SHA；新增已執行的測量為零。這四次片段回放只核對工程回呼，沒有寫回原 Run、重試科研席或取代初次表現。

```sh
GX_CODEX_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  python -B -m unittest -v acceptance.harness_final.evidence.test_bibliography acceptance.v621.test_verified_metrics acceptance.v624.test_selected_rows
```
