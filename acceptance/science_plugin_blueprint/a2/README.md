# A2 原文適用條件與研究員交接

本案的問題是換路燈後，居民能否在整夜看見更多星星。問題、評分條件、起始圖集、同 DOI 的出版索引，以及後到研究的取得位置與雜湊均在本目錄。取得程式把 NCBI 的 XML 信封解出為 JATS 文章。Crossref 項只投遞題名、DOI 和出版者的摘要文字。兩篇文章均採非商業性署名授權。[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)。[Falchi 等，2016](https://doi.org/10.1126/sciadv.1600377)。[Kyba 等，2017](https://doi.org/10.1126/sciadv.1701528)。

## 執行

在產品根目錄執行下列命令。`study_dir` 指向獨立研究工作區；本案首次執行使用相鄰的 `gx-science/plugin-blueprint-live/a2`。執行後的原件放在其 `materials/`，原始資料保持上游條款。

```bash
study_dir=../gx-science/plugin-blueprint-live/a2
case_dir=acceptance/science_plugin_blueprint/a2
.venv/bin/python -B "$case_dir/prepare_materials.py" "$study_dir"
cp "$case_dir/goal-config.json" "$study_dir/config.json"
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study_dir" research \
  --state "$study_dir/state" --run-key skyglow-a2-first \
  --goal-file "$case_dir/question.md" --goal-config "$study_dir/config.json" --timeout 1080
```

在第二個終端，同一 Run 的資料庫出現後執行後到材料投遞。偵測到已保存的第一個原文閱讀回執才會送出一次持久 PI 訊息。重送使用相同訊息 ID。

```bash
study_dir=../gx-science/plugin-blueprint-live/a2
case_dir=acceptance/science_plugin_blueprint/a2
.venv/bin/python -B "$case_dir/deliver_later.py" "$study_dir" > "$study_dir/delayed.json"
.venv/bin/python -B "$case_dir/collect.py" "$study_dir" > "$study_dir/receipts.json"
```

需恢復時，在相同研究工作區改用 `research --state "$study_dir/state" --run-id <Run_ID>`。官方 OAuth 宿主透過 Clash。收集器僅讀既有資料庫與官方回合回執，交回生效決定、研究模組原話、GX 產物事實、原文段落、涵蓋範圍與獨立裁決。

## 離線回歸

```bash
PYTHONPATH=gx-h:. .venv/bin/python -B -m unittest tests.test_researcher_handoff tests.test_v62_sources tests.test_v62_lead_delivery tests.test_v62_modules_publication tests.test_v62b_handoff tests.test_v62_module_funding
```

兩介面對同一 DOI 的供應記錄可保有不同位元組與不同涵蓋範圍。檔內 `gx_read_source` 定位結果可分頁，再讀指定段落取得個別任務的閱讀回執。研究員的署名內容、GX 自動補交的缺口與具名 Job 的事實分欄展示。簡報限長時已有原文報告連結；獨立複核需另外一個原生上下文讀到指定原件。
