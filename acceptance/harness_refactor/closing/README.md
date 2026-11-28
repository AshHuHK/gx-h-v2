# 收尾、主張與 PI 簽署驗收

原始 PM₂.₅ 研究缺少指定兩台儀器的本地配對資料。封存 Run `gxr_01m8fd9bzcpwt0qr1n` 收錄 160 篇候選，41 篇標為開放，9 篇全文有定位閱讀，共 806 筆段落回執。在原決策及同一決策明示 `INSUFFICIENT_DATA` 的只讀重放中，GX 均提示 0 次，新增 0 個 Job 與 0 個模型回合。[回放與原決策](pm25-replay.json)

另一個受控研究在普通 `gx-p0 research` 入口帶入一份本地配對資料。子研究員讀取材料、登記報告與核心對照問題的關係並交回報告。頂層以缺少本地對照準備結題，GX 回傳一份已送達的報告引用。Lead 在同一回合讀到完整報告、修訂局限並實際結題。傳輸端使用受控 Codex 回覆。科研動作經過產品的委派、回執、決策與結題程式。[Run、模組、報告、提醒與結題決策](paired-report.json)

| 能力 | 驗收 |
|---|---|
| 正常入口與相容性 | 基線 `50d31f5` 的正常入口拒絕具名關鍵資料設定。新契約由 `gx.harness/2` 釘住。既有閉案與原生工具測試一併復跑。[基線](first.log) |
| 提醒與重啟 | GX 用科學 stop_reason 和明確的核心問題查找關鍵材料，原生回調將提醒送回仍在運行中的 Lead。重送同一 call 得到原回執，重啟後再次提交可結題。科學完成、中性未指定材料及已核對的等價段落可直接結題。[回歸](targeted.log) |
| 原件與等價性 | 只讀尾段無法代表已讀完整檔案。同一出版物的等價定位須有相同段落雜湊與區域。另一論文複製相同文字、同篇修訂後改寫方法，都不會清除原件待查狀態。[測試](test_closeout.py) |
| 候選與實際取得 | 使用原 PM₂.₅ 資產另外做反事實點名，分辨候選、上游聲稱可取得、取件失敗及已有定位閱讀。失敗可追至 `gxj_01m8fdvefnz9k91fqw`。回放唯讀。[實際候選與取件 Job](candidate-states.json) |
| 主張證據 | 正文方法等級須有具名正文方法段的閱讀回執，或該段具定位的原文方法摘錄。摘要、無關結果段與零模型作業均會被拒。已核對的回執進入正式主張修訂及 JSON 報告；科學支持方向沿用原有主張評估。[測試](test_closeout.py) |
| 模型計算與獨立打分 | 歷史 Run `gxr_01m8f5r59yyx9gmekh` 的模型 Job 與評分 Job 是兩個具名作業。每個分數的預測輸入及觀測參照均能追到原件。把殘基映射檔替換成觀測參照的申報遭拒。本次重放新增 0 個模型回合及 0 個 Job。[原作業與產物雜湊](model-grade.json) |
| PI 簽署 | 普通科研可在未簽署時結束。已約定的簽署在報告中獨立展示。本機操作員只能對已生效的精確結題決策簽署。重送只留下首次簽署。[測試](test_closeout.py) |

## 使用方式

新研究可用可選的 `closing_terms`。例如把下列欄位加到已有對應 `input_files.role` 的 Goal config。省略此設定仍可正常研究。

```json
{
  "closing_terms": {
    "core_materials": [{"role": "local_pair", "question": "local controls"}],
    "signoff": "PI 核認具名的配對比較與報告限制。"
  }
}
```

需要指名簽署人時可在同一物件指定 `signer`，值須與本機 GX 操作員身分相同。Lead 或模組也可於研究中呼叫 `gx_link_core_material`，點名已可見原件、搜尋候選或受委派模組並說明其重要性。具名為必需的 Join 交付也可透過原問題與已送達產物連結。對資料不足的結題提交使用 `stop_reason=INSUFFICIENT_DATA`，特定子問題可在 `close_run.core_question` 引用原問題。提醒後由 Codex 選擇補讀、維持局限或改變下一步。

PI 已核認正式結題決策後，以下命令會留下其身分與精確對象。重新匯出即可檢視簽署及已封存的科學結果。

```sh
bin/gx-p0 operator sign-off --state "$STATE" --run-id "$RUN" --decision-ref "$DECISION"
bin/gx-p0 research --state "$STATE" --run-id "$RUN"
```

## 復驗

在完整產品根目錄執行。前兩個重放使用現場歸檔資料庫，最後一個使用已有的模型校準資料庫。這些命令沒有網路請求。

```sh
export PYTHONPATH=.:gx-h:gx-h/tests
.venv/bin/python -B -m unittest -v acceptance.harness_refactor.closing.test_closeout acceptance.v622.test_close tests.test_v6_claims tests.test_v62_sources tests.test_p0_local_operator tests.test_v61_native_contract
.venv/bin/python -B acceptance/harness_refactor/closing/pm25_replay.py ../gx-science/asset-live/literature/state/gx.db acceptance/science_assets/literature/result.json
.venv/bin/python -B acceptance/harness_refactor/closing/candidate_states.py ../gx-science/asset-live/literature/state
.venv/bin/python -B acceptance/harness_refactor/closing/model_grade_replay.py ../gx-science/science-plugin-blueprint-b0-release/state
```
