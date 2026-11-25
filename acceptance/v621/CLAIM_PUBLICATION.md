# GX 的主張發布等級

使用 `gx.evidence/6.2.1` 並啟用成熟度裁定的 Goal，以採納時的同一命題、適用域、已註冊的量測與事件快照判斷方向。原生工具回執、下一輪 Lead 簡報、`gx-p0 status` 和對外匯出使用同一個 `claim_revision_id`。

| GX 對該命題的判定 | 允許發布的等級 |
| --- | --- |
| 方向未定，含沒有可用的端點讀數 | open、insufficient |
| 證據支持 | open、insufficient、limited_support |
| 證據反對 | open、insufficient、contested。前瞻註冊的端點已測且反對時可選 refuted，僅涵蓋原範圍 |
| 同時有支持與反對 | open、insufficient、contested、mixed |
| 多份已註冊材料只有部分有適用讀數 | open、insufficient。有反對讀數時亦可選 contested |

獨立審閱的文獻可以在其已審適用域內支持或反對命題。觀測和分析用作實證時，GX 核對原始取得紀錄和適用域。不同端點、不同適用域和資料未測的情況各有可追溯的收據。

舉例而言，當已註冊的北站晨間溫度讀數反對預測，Lead 提出的 `limited_support` 會以 `contested` 發布。`publication` 記錄可發布等級、支持和反對的引用，以及提案超出證據的理由。`rationale` 說明正式判定；`lead_interpretation` 保存 Lead 的理由和原提案引用。`proposal_ref` 指向原始提案，尚未觀測的材料列在 `unmeasured_data_refs`。後續讀數更新主張時，GX 新增修訂；原裁定和當時的證據保持可重播。Lead 可依不同條件提出範圍較窄的後繼命題。

V6.2 的舊 Run 重播原始收據，保留原先的 `science.level_source`。新的 quick Goal 維持原有的成熟度設定；帶具名量測的數字仍經 GX 渲染。

執行範例可從 checkout 根目錄運行 `PYTHONPATH=.:gx-h python -m unittest -v acceptance.v621.test_claim_publication`。可選的 `GX_V621_CLAIM_RECEIPTS` 指定保存每個案例的修訂、讀數、事件與簡報摘要的目錄。
