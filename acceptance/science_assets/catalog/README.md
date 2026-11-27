# 歷史資產在普通 Goal 中的可見性

受控原生回呼在普通研究入口建立 `gxr_01m8fdhzqnx1rkxwha`。原始狀態在產品相鄰目錄 `../science-assets-catalog-8NtEvt/state`。本案使用實際部署的資產索引，模型回合數及外部 Job 數均為零。

| 詢問或嘗試 | GX 的實際回應 |
|---|---|
| 用跨類搜尋、方法搜尋及自然中文查歷史結構模型 | 均列出 `protenix-model@legacy`。詳細卡可查到來源表、列號、雜湊、歷史 provider 和描述性輸出。 |
| 嘗試啟用及提交該歷史方法的 Job | 兩種操作均回覆 `SCOPE_DENIED`。決策的拒收原因是方法不在該 Run 凍結的可執行目錄內。 |
| 搜尋 `pdb-archive` 資料資產 | 返回兩份歷史入口，各自保留 provider binding，共用科學操作 ID。覆蓋為檔案檢索的歷史登記，本案未向資料庫發送查詢。 |

15 張遷入卡的原案保留。索引擴到 102 張、摘要 `8405f8c2…` 後，另在 `../gx-science/asset-live/catalog-expanded-umIODS/state` 建立 Run `gxr_01m8fdxztdt257hbeh`，普通入口重驗成功。兩次拒絕的決策、歷史 provider 對照以及各查詢的分母均隨回執保存。

校正跨來源關聯後，發布索引摘要為 `c7ab6949…`。另用新 Goal `gxr_01m8fe7ybfhqn9xt7y` 查詢、讀卡及嘗試誤派工，私有狀態在 `../gx-science/asset-live/catalog-final-lBEBfH/state`。[發布索引回執](final-field.json)

[首次 15 張](field.json) · [擴充後 102 張回執](expanded-field.json) · [首次故障](first-run.log) · [定向回歸](discovery-tests.log)

專項測試另外在私有配置副本中加入、重複登記和撤回資產，驗證同一 Run 只收到有效變更，冷啟動後仍能找回舊回執。索引 JSON 損壞或某列缺少可用名稱時，正常來源繼續回傳並列出未讀部分。具備公開方法調研權限的子研究員也能精查歷史方法。其 scope 和 Job 權限維持原來的範圍。沒有部署選用索引的舊工程沿用原生舊目錄介面。

在產品根目錄復現時使用新的研究目錄，以保存各次原始結果。

```bash
PYTHONPATH=gx-h:. .venv/bin/python -m unittest acceptance.science_assets.catalog.test_assets
PYTHONPATH=gx-h:. .venv/bin/python -m acceptance.science_assets.catalog.probe \
  ../my-new-catalog-study /tmp/my-catalog-results.json
```
