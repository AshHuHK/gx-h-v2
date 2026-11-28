# B 首次現場研究

在 `362157ae2a6c6f723376dc476b2d5f6c2b001548`，普通 Goal `gxr_01m8g0ad2w0vh088y0` 由官方 Codex 以現有 OAuth 啟動。首次研究從 02:39 至 02:56 UTC，9 個官方已回執回合、1 個新 GPU 作業、1 個新 CPU 評分作業。研究問題給了成熟天然乳蛋白材料。Lead 在事件 19 辨認限於釋出前的階段約束，同一回合事件 20 查詢方法，為這條鏈真正建立新的預測和評分 Job。[場測時間線](timeline.json) · [固定發行與原件摘要](field-condition.json)

| 步驟 | 實際結果 |
|---|---|
| 選法與計算 | 事件 44 接受序列預測。GPU Job `gxj_01m8g0d3d2amp59bj4` 用站點 AlphaFold 3 空 MSA、空模板、無顯式鈣離子、seed 1、10 recycles 和 1 sample 產生新座標。NVIDIA H800 報告 260.20 秒模型推理、297.24 秒 Job 耗時、321.214 CPU 秒；分配 16,384 MiB。共享裝置峰值增量 2,699 MiB，工具單獨分配器峰值未知。[模型座標](model.cif) · [方法與信心](inference.json) · [輸出條款](TERMS_OF_USE.md) |
| 斷線接管 | 科學 provider PID 1934293、start ticks 3015309446，實際開啟 `/dev/nvidia4` 後只讓原 GX Driver 1924169 退出。新 Driver 1936464 取得原 workspace 寫鎖時，觀察到相同 provider 仍開啟同一實體裝置。至作業成功始終為同一 `runner:1934286:3015309326`，原生程序僅有一次啟動、一次成功退出，重啟後額外推理為 0。[中斷](driver-recovery.json) · [新 Driver 實際接管](running-after-restore.json) · [原生程序日誌](provider-events.jsonl) |
| 盲化與獨立評分 | 新模型在事件 77 發布。保管席在事件 94、98 才投放晶體原件與對照表；投放前記錄的官方模型會話沒有金標 accession 或所封存的檔案摘要。CPU Job `gxj_01m8g0v91xknqrv75g` 以 1F6S chain A 的實驗座標評分。123 個對照位置中 122 個可比，實驗未觀察到第 123 位 Cα。Cα RMSD 為 12.287726080536236 Å。獨立讀取 legacy PDB 後用 QCP 得 12.287726080536238 Å，兩者差 1.78×10⁻¹⁵ Å。[原始評分](agreement.json) · [獨立複算](independent.json) · [投放記錄](custody.json) |
| 研究決定 | 事先列出的近全鏈門檻為覆蓋率至少 90%、RMSD 至多 2 Å。事件 165 的 Lead 決定引用具名讀數，判定此預測未達所測鈣結合沉積的全鏈相符門檻。Lead 主動詢問無鈣或局部參照，保管席依據預登記庫存答覆僅有此次釋出的材料。事件 185 再送達三個具名讀數；Lead 決定 193、封存事件 195。單個晶體無法判斷誤差起因、局部區域準確度、食品加工行為或功能。此公開沉積可能出現在模型的訓練資料內。[正式決定](DECISION.md) · [正式報告](REPORT.md) |
| 原 Run 重導 | `research --run-id` 冷重導保留正式報告、決定每一位元，新增 0 個 Job、0 個官方回合。[重導收據](cold-export.json) |

## 原結果中的限制

GX 有 11 次拒絕提交的回執，包括分支、一般結題欄位、已撤銷的舊 turn 以及尚未送達新 turn 的 endpoint。最終讀數、正式決定和這些拒絕都保留於全程研究紀錄。已發布 JSON 只有呼叫命令的本機工作目錄改成可攜寫法。[完整執行紀錄](result.json) · [原始輸出摘要](result-export.json)

GX 的兩筆原始預測自動裁決都為 `registered_without_qualifying_result`。評分輸出繼承了晶體與位置對照表各自的 applicability；這兩條描述令輸出未滿足現行的單一字串精確匹配。第二份登記已明確選用 reference 輸入與晶體的 applicability，仍留下 `out_of_scientific_scope`。本次成功的是新模型執行與原 Job 接管、獨立量測、Lead 學術判斷與封存。原始預測的自動裁決達成 0/2。獨立簽收的數值不更動產品保留的原判。

## 從發行材料複算

這條命令從已交付的座標、位置對照及來源原件重新進行 QCP 比對，無需 GPU、網路、舊專案或本次私人 SQLite。所需 Python 依賴為 Biopython 與 NumPy。

```bash
.venv/bin/python -B acceptance/harness_final/gpu/verify.py --released
```

[乾淨材料複算收據](release-replay.json) · [釋出的原件及取得說明](../released-originals/README.md)
