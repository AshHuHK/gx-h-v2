# 科研資產與四類卡的使用方式

一個酵母快照可以回答「這個版本登記了哪些反應」。要決定兩個菌株的催化活性誰高，還需要同條件的量測。把數據、如何查數據、如何判讀和要解答的問題分開，Lead 才能在缺一種證據時仍完成其他工作。

| 物件 | 適合建卡的情境 | 本版接在哪裏 | 科學責任 |
|---|---|---|---|
| 任務卡 | PI 想對不同材料重問同一問題，並保持相同分母和結題條件 | 普通 `research --task-card FILE --goal-config FILE`。卡保存問題、驗收條件、停止條件和可選 effort。每題的輸入及 scope 在 goal-config 綁定。原有 `--goal`、`--goal-file` 均可繼續使用 | 自主選法題只給要區別的現象。指定復現題可以記所需方法。實例化後按既有 Goal 保存原文，更改檔案另開題；已開題從 Run ID 恢復 |
| 方法卡 | 有一種可獨立執行、能產出具名結果的操作，例如以反應、物種和快照版本查註釋，或在坐標內計數 | 插件 manifest 聲明輸入、輸出性質、方法版本及提供者。目錄預覽和 Job 綁定使用同一組已聲明條件。角色尚未選定、應用域無法從材料辨認時回報 unknown | 版本和已量測範圍由各自的 Job 回執保證。舊模型、服務名稱或只有輸入準備的工具可留參考卡，不能據此外推已進行推理 |
| Skill | 不同題經常遇到同一判斷，例如少數可比位點能否代表全鏈，或 EC 類註釋能支持何種敘述 | `gx_query_catalog(kind="skill")` 搜短卡，再以 reference 精查正文。新卡 `interpret-coordinate-coverage@1` 與 `interpret-enzyme-annotation@1` 都有具名依據、使用情境及限制 | 瀏覽短單、打開精確版本、引用理由和後續行動分別取證。Lead 可採用、放棄或讀後維持；同一 skill 不需要綁定某個唯一的 Job |
| Recipe | 已執行的步驟要改用另一份輸入，並在新讀數到來後再次判斷 | 由來源 Lead 對成功節點 `gx_save_recipe`，後題用 `gx_query_catalog`、`gx_instantiate_recipe` 和正式決策採用。版本、提供者、具名埠、輸出性質與待答決策點隨配方保存 | 明列工作流起點、必需步驟、可選步驟及結束點。審閱整條是否已在兩題執行。當原工作只是其中一段，簽收這段子圖 |
| 資料資產 | 可重問的來源或一組觀測，例如 Rhea release、PDB 沉積、某篇研究的原件 | 目錄保存來源、版本、取得介面、樣本與已知覆蓋。實際查詢走方法卡，回傳的行或原文是當次產物 | 同一發表透過兩個介面取得仍是同一科學來源。必須區別零行、缺欄位、快照缺失、資訊無法支持結論 |

## 從普通科研入口重用

以下命令在完整或發行候選根目錄內執行。任務卡與材料可分別更換。這個示例用兩份公開沉積界定可觀測坐標範圍。

```bash
. .venv/bin/activate
bin/gx-p0 research --state /path/to/new-study --run-key coordinate-comparison \
  --task-card acceptance/science_assets/cards/coordinate-coverage.json \
  --goal-config acceptance/science_assets/modalities/studies/linear-peptides/goal.json
bin/gx-p0 research --state /path/to/new-study --run-id RUN_ID
```

若後題需要消費前題的計算產物，將已結題 Run 的具名輸出寫進 `--successor-config`，由普通 `gx-p0 message --successor` 遞交。下列欄位值取自該 Run 的正式 Job 回執。新 Role 由後題研究需求決定。

```json
{
  "parent_outputs": [{
    "job_id": "<來源 Job>",
    "output_name": "parent-records.json",
    "artifact_ref": "<來源產物>",
    "sha256": "<來源產物的完整 SHA-256>",
    "role": "prior_records"
  }]
}
```

GX 在父題結題時可見的已發布產物中核對 Job、埠、摘要，再給後題一個本地 artifact ref。來源、讀數性質和已知時間保留；這份複本在統計上不增加樣本。重送同一條後繼訊息會接回同一題。研究者仍須決定要不要使用它。

## 插件作者怎樣接進來

1. 資料只有舊入口時，先記來源、快照與操作身份。只讀歷史方法可在 `kind=method` 與 `kind=all` 查到。方法名稱相同但提供者不同時分列。機讀舊資產卡中的 `provider_binding` 是歷史來源，本題執行權看當前 pin、scope 和可用性。
2. 實際可執行的操作放進既有 `gx_plugin.json` 與 method manifest。對每個具名輸出分別聲明作用和性質。庫掛載交由部署解析，發版資料內保留可檢查的快照身份。資料庫中未找到與插件未能開庫，要傳回不同的結果。
3. 使用現有 `bin/gx-p0 plugin digest/install/verify`，從一個普通 Goal 選法並派出 Job。用不同的公開輸入、缺資料負例以及獨立讀取核對宣告。若方法源自同一執行模板，保留模板及提供者 pin。
4. 有可複用的科學判讀再寫 Skill。要重用成功步驟才發布 recipe。查到一張卡、安裝成功和實例化配方都各有回執；只有實際成功 Job 才計入執行。[方法欄位與反例](contracts/README.md) · [舊資產賬本](inventory/README.md)

兩位真 Lead 實際打開本輪的新 Skill。酵母父題精讀註釋判讀卡一次，Fab 父題精讀坐標覆蓋卡三次，之後各自派出 Job，並在結題時限縮可成立的主張。兩份決策均未逐字引用 Skill 識別，因此只簽收已閱讀及後續動作。[十題的短單、精查、原生回合與行動](cards/SKILL_FIELD.md)
