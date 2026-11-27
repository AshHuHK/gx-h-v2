# 天然酵母酶的歸檔查詢與跨題對照

`org.gx.yeast-enzyme-archive@1.0.0` 在普通 Goal 提供麥芽糖、異麥芽糖、海藻糖和乳糖四種 S. cerevisiae S288C 糖水解歸檔查詢。既有 `org.gx.yeast-enzyme-evidence@1.0.1` 仍保留原先的海藻糖和蔗糖範圍。另一次普通 Goal 指定該舊方法查麥芽糖，實際退回範圍錯誤，0 個 Job。[舊方法回放](legacy-scope-control.json)

| 對象 | 本次的使用方式 |
|---|---|
| Task | 第一題交付酵母與糖種。後題改糖種並登記前題正式發布的資料。判斷酶活、菌株特異性、同一實驗是否獨立時，允許保留未知。 |
| Method | `yeast-archive-lookup@1` 讀取正式掛載的四個 SQLite，交付 `parent-records.json`。基質類別、精確反應關聯、單一或多 accession 的測定分開計數。 |
| Skill | 遇到專論或測定歸屬歧義時，Lead 可從現有閱讀技能回到原文。本插件交付的資料表定位只證明該快照中的記錄。 |
| Recipe 與跨題交接 | 父題成功的查詢可透過既有配方入口保存。後題的 `yeast-archive-compare@1` 同時讀取新 query 和本題已登記的前題具名產物，核對存檔身份，再輸出兩端 accession、序列版本／摘要、範圍及差異。配方發布、發現、實例化與派 Job 的現場回執各自記錄。 |

從完整研發版或精簡版的產品根目錄綁定資料。相同的檔案內容可在不同路徑掛載。遺失資料會報缺少來源，換了資料會報快照摘要不符。

```bash
.venv/bin/python -B acceptance/science_assets/enzyme/mount.py \
  --harness-root gx-h --source /path/to/science-native-v2/var/enzyme-assets
bin/gx-p0 plugin verify org.gx.yeast-enzyme-archive@1.0.0
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v \
  acceptance.science_assets.enzyme.test_archive acceptance.science_capabilities.test_yeast_evidence
```

安裝不會複製 BRENDA 等上游資料。歸檔文件保留於原讀取範圍；套件固定四個來源摘要。第五庫 Horizyn 與 FASTA、BLAST 的核驗另附，其結果不作本方法的讀數。新 query 可用 `{"schema":"gx.yeast-archive-query/1","taxid":559292,"substrate":"maltose"}`。後繼題透過 GX 的 `parent_outputs` 建立本地引用，提供 `query` 與 `prior_records` 兩種材料。兩個方法的參數均為空物件。[方法原件](../../../gx-h/plugins/installed/org.gx.yeast-enzyme-archive/1.0.0/gx_plugin.json)

## 可驗收的資料差異

Rhea 142 和 Swiss-Prot 2026_03 在本菌株的兩種新反應精確關聯均為零。另以 ENZYME 2026-01-28 的分類查詢，得到 2 與 5 個天然 accession。麥芽糖反應 master 68796 所列 EC 為 3.2.1.48；補查的酵母 maltase 類別是 EC 3.2.1.20。保留這一差異，才能區分「找得到一個類別成員」與「原件指明這個反應」。

異麥芽糖的五個成員在 BRENDA 快照中連到 107 個相異條目，其中 92 個為單 accession，15 個屬分組。14 個條目在化合物欄精確命名異麥芽糖，其中 3 個屬抑制常數 Ki。限於 Km、turnover、kcat/Km 的底物欄，為 **11 個條目（10 單一、1 分組）**。兩糖種在這份 SABIO-RK 快照的成員 accession 連結均為零。菌株歸屬各有證據強度；本文沒有取得新實驗活性、動力學預測或工程化變體結果。[獨立原始解析與實物遷移](sources/README.md)

受控普通入口在同一 Run 接收庫外糖種後拒絕排程，接著真正執行對乳糖的正常 CPU 查詢並得到零命中。海藻糖陽性對照有 3 個精確 S288C 關聯，三個方向均為 UN（未指定）。[受控 Goal 與實際 Job](goal-control.json) · [定向測試](regressions.log) · [真研究預先規定的問題與條件](field/preregistration.md)

## 實際工作流邊界

首題的一步查詢由 Lead 自行保存為 `s288c-disaccharide-archive-qc@1`。C 查到並實例化該步驟，自己的異麥芽糖查詢與跨題對照均取得新 Job。對照 Job 的兩條實際輸入是 C 的 query、由 A 轉交且在 C 已登記的 `parent-records.json`。它獨立查庫核對兩端。C 同時新增的單題 lookup Job 有自己的可追溯產物，因此 C 存在一次對新糖底層資料的重查；該 lookup 的輸出未作為 compare 的輸入。可簽收的重用包括父題已做的一步配方與 A→C 的正式產物交接。[普通研究入口與現場回執](field/README.md)

A `gxr_01m8fdke95wxpbgs7j` 於事件 78 採用研究決定，80 結題。C `gxr_01m8fdw087fqx283nq` 在 177 採納主張、183 作出正式決定、184 結題。兩題各 3 個正式模型回合，分別執行 1、2 個 CPU Job。C 決定分別記錄這兩種糖的分類成員與測定歸屬：兩組合計 7 個 accession、6 個不同序列摘要，IMA3 和 IMA4 序列相同，測定指向的 accession 仍須分開。這是自然記錄的跨題審核；工程化親本或新酶設計未納入此次實跑。
