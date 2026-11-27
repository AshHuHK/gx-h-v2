# 方法卡的科學適用範圍

研究者面對兩份資料時，方法有沒有安裝、Goal 是否已准入、資料能否回答這個方法的問題是三個獨立判斷。目錄保留原有安裝和執行欄位。短卡與精查卡的 `input_coverage.domain` 給出已宣告範圍和本次可見材料的比較。可見材料中的角色名對上時仍會檢查具體條件。多份同角色材料待 Lead 選定，因此在預覽標為 `unknown`。依賴節點尚未交付材料時可先登記圖，真正有輸入時依照同一規則再次判定。

## 插件作者

能力包已有 `manifest.method_card.applicable_domain` 和 `manifest.applicable_domain`。前者表達方法本身的條件，後者表達所選 provider 的包裝範圍。只需在有可執行判斷依據時填入 `gx.science-domain/1`。兩層都宣告時取交集。每一條輸入角色對應 manifest 的具名輸入埠。這段示例描述接受哪種儀器檔案。

```json
{
  "schema": "gx.science-domain/1",
  "inputs": {
    "record": {
      "json_fields": {
        "schema": {"one_of": ["gx.hydrometry/1"]},
        "channel_count": {"min": 1, "max": 2},
        "flags": {"subset_of": ["audited"]}
      }
    }
  },
  "randomness": {"mode": "deterministic"}
}
```

- 固定樣本用該角色的 `sha256` 白名單；可變輸入可以用 `media_types` 或 JSON 原件中的屬性。欄位名對應實際輸入文件頂層鍵。字串類別用 `one_of`，整數上下界用 `min`、`max`，修飾或旗標清單可用 `subset_of`。例如已歸檔的結構描述若提供分子類別、鏈數、拓撲、長度及修飾，可以按相同方式宣告支持範圍。原始 mmCIF 的鏈、編號和連接仍應由領域 parser 從原件核對。
- `randomness.mode` 可為 `deterministic` 或 `stochastic`。可填 `fixed_seed` 或指向已有參數的 `seed_parameter`。參數、輸入雜湊與執行環境繼續由原 Job 收據記錄。`independent_repeat` 沿用原圖語義。每個輸出分別保留 manifest 的 `name`、`kind` 及可選的 `nature`。
- `supported` 表示可核對的材料已符合所有已聲明條件；`unsupported` 列出明確不符的條件；`unknown` 列出缺失的證據。預覽基於可見且角色唯一的候選，按請求重用讀到的原件；廣泛搜尋不解碼超過 1 MiB 的 JSON，確定綁定才讀完整輸入。材料未指明 MIME type 時，不由副檔名猜測。舊卡缺版本化條件仍可照原方式運行，亦保留其原授權。
- 若插件在綁定時產生或重排輸入，GX 以實際綁給方法的各角色再判定。未知且未提供那項必要事實，或輸入已明確不適用時，保留原因並阻止該項 Job。宣告、推論和原件的實驗性質各自留在其原位置。
- 閱讀卡或只讀歷史方法即使列出可用輸入仍要有真實 provider 和 Goal scope 才能派工。方法宣告的文字描述與可執行範圍各自按版本凍結。

## 驗收

原版短卡缺少資料適用性字段。九個定向案例涵蓋舊卡、合適／不合適／未知、安裝了無效域的不相關包、尚未產生的上游輸入及重新綁定後的實際材料。兩節點案例在同一普通 Goal 採納等待中的節點，拿到上游具名輸出才發現它缺少必需的科學欄位；第一個 Job 成功、第二個節點保留失敗且沒有派出第二個 Job。正常 `gx-p0 research` 搭配受控原生 Lead 派出一個真正 CPU Job，從兩通道資料得到 85 分鐘共同時段和 4,740 L 的體積差。[首次失敗](first.log) · [正常入口的 Job 和產物回執](ordinary-goal.json) · [定向測試](domain.log) · [受影響的既有案例](affected.log)

在完整產品目錄重跑。

```bash
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a GX_CODEX_OFFLINE=1 .venv/bin/python -B -m unittest -v acceptance.science_assets.contracts.test_domain
```
