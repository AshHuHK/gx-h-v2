# Lead 是否實際使用本輪兩張 Skill

從 10 個當期真實 Goal 的 GX 資料庫讀取原生目錄回執，並用本機官方會話的 provider、thread 與 turn ID 核對研究員身分。另讀上一輪天文研究作為歷史對照。在有兩張新卡的真課題中，各有一個 Lead 實際展開該卡，之後在同一回合提交了有具體 Job 效果的決策。兩份正式決策都沒有逐字引用 Skill ID 或卡片摘要。問題本身和原件也可提供相同的判斷線索，因此只能證明曾選讀、後續理由與指南相符。[逐呼叫與正式決策](skill-receipts.json)

| 當期案例 | 短單／完整卡 | 之後實際發生的事 |
|---|---|---|
| 酵母父題 `gxr_01m8fdke95wxpbgs7j` | `interpret-enzyme-annotation@1` 在 Lead 的返回頁面出現 4 次。Lead 精查 1 次，卡片 SHA 前綴 `f89d999be474`。 | 同回合事件 34 採納查找 S288C 麥芽糖反應原件與 assay 歸屬，派出 CPU Job `gxj_01m8fdmbx53g9thef6`。60 等待交付，明載同一 assay 經兩個介面仍只算一次。78 結題時分開交付 MAL12／MAL32 的分類來源、缺反應連結與測定底物的限制。 |
| Fab 父題 `gxr_01m8fdh3yjvwr7ph5s` | `interpret-coordinate-coverage@1` 在返回頁面出現 6 次。Lead 精查同一版 3 次，SHA 前綴 `5b58d82fcd48`。 | 事件 46 已在首次打開卡之前發生。首次精查之後的 79 採納成對 IMGT 讀取，派出 CPU Job `gxj_01m8fdt5txksns88fc`。107 保留在途結果。128 依照六個 CDR 的事前分母（VH 8/10/9、VL 11/3/9）及實際 Cα 掩碼結題，把可比性限於共有 IMGT 位置的 Cα 軌跡。 |
| 其餘來源 | 酵母後題、Fab 後題、線性肽、環肽、蛋白座標控制父／後題和精簡版肽例的快照中，有新卡入短單，精查均為 0。文獻研究於此輪快照未見兩張新卡。 | 這些題各自的科研決定保留；不能因為問題跟卡相似就歸功於該卡。開放中 Goal 使用各自快照的最大事件 ID。 |

新卡在目錄中的狀態為 `reference_only`。Lead 能讀其指南、適用範圍與來源，另從已准入方法派工。獨立查核保留完整卡的 `call_id`、已驗證的回合、卡片摘要，以及後續正式決策、實際節點、Job ID 和結果引用。這個場驗沒有新增推理或作業。

歷史天文題 `gxr_01m8f7ypt5cm2e6rkq` 主動打開 `source-evidence-review@1`，並依原文保留對跨國 GDP 增長排序的限制。該次真回合和舊的受控精查證據另行列示。[歷史真模型與受控對照](../../science_plugin_blueprint/reading-field/README.md)

要以現有狀態重新取證，在完整產品目錄執行以下命令。程式對每個資料庫開唯讀快照，僅識別所列研究工作區內確定屬於該 Run 的官方會話，未進行外部推理。

```bash
python3 -B acceptance/science_assets/cards/skill_evidence.py --output /tmp/gx-skill-field.json
```
