# 兩個天然酵母檔案研究的普通入口

第一題替食品資料庫審查 S288C 麥芽糖條目的證據層次，第二題對另一糖的歸檔資料重新計數，再與第一題實際交付的文件比較。兩份 PI 題面都只給研究問題。准入配置按部署允許公用資料庫與 CPU 分析；Lead 在現有目錄自行選用方法。材料為物種與底物的查詢範圍，沒有內置答案。[麥芽糖問題](question-a.md) · [異麥芽糖問題](question-c.md) · [首輪前判準](preregistration.md) · [入場前五份內容摘要](preregistration.json)

在已安裝插件與酵母索引掛載的完整研發或精簡部署，從產品根目錄執行。工作區使用本機 Clash 及既有官方 OAuth。

```bash
study=../gx-science/asset-study-live/yeast
field=acceptance/science_assets/enzyme/field
.venv/bin/python -B "$field/run.py" "$study"
.venv/bin/python -B "$field/run.py" "$study" --successor-of "$PARENT_RUN"
.venv/bin/python -B "$field/collect.py" "$study" > "$study/receipts.json"
```

首題以穩定的 run key 入場。結題後，第二條命令取首題成功 Job 的具名文件和不可變摘要，以穩定的 PI 訊息建立後繼 Goal。後繼在同一狀態庫取得新的本地產物引用。重開已准入研究可直接使用既有案號。

```bash
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study/host" research \
  --state "$study/state" --run-id "$RUN_ID"
```

源快照、版本、對照分母和獨立 SQL 檢查保存在酵母資料資產的驗收目錄。[來源與獨立數值參照](../sources/reference.json)

## 實際首跑（2026-11-27 UTC）

兩題各由三個可與官方 Codex 會話匹配的 Lead 回合完成。當次模型為 `gpt-6.2-astra`、effort `max`，沿用現有 OAuth。這次沒有委派子研究員。兩個 Goal 都固定了同一個插件內容 SHA-256 `826f8604c13df559737834134e896f7b96d456c2318ac82f3c18a8dd035d1ad6`。開發工作樹在研究期間另有整合提交，因此證據記錄的是實際合約、插件及輸入摘要；單一提交的獨立重部署須參見另外的發行驗收。[兩題回執](receipts.json) · [獨立驗算](verification.json)

| 首次研究 | 普通 Run | 真實 CPU Jobs 與首次結果 | 決定與生效 |
|---|---|---|---|
| A 麥芽糖 | `gxr_01m8fdke95wxpbgs7j` | 查詢 `gxj_01m8fdmbx53g9thef6`，事件 51，入場後 30.815 秒 | 決定 78，事件 80，21:16:31 UTC 結題。[A 完整理由](parent/DECISION.md) |
| C 異麥芽糖／跨題對照 | `gxr_01m8fdw087fqx283nq` | 新材料查詢 `gxj_01m8fe211d7m5avz56`，事件 157，入場後 198.147 秒；雙輸入對照 `gxj_01m8fe218m9yw27h80`，事件 158 | 決定 183，事件 184，21:25:07 UTC 結題。[C 完整理由](successor/DECISION.md) |

A 保存了 `s288c-disaccharide-archive-qc@1`。C 找出並展開該配方，以自己的查詢新派一個帶原 recipe digest 與 `lookup` 步驟標記的 Job。C 先後展開兩次，實際執行一次，並在結題引用新輸出。C 另選雙輸入方法，將 A 的具名成品與 C 的新查詢送入比較。該方法自行重查當前底物，與 C 的查詢 Job 存在一部分重複讀庫工作。事件 95 保存原生產者、摘要與新身分。A 原檔 `gxa_01m8fdmcaez3kstbtj` 轉入 C 為 `gxa_01m8fdw094p69b59nq`，兩者 SHA-256 均為 `6ac837e862e61b7ea98f7a7374340813bd1f8b23f713eccd74eb9aca39a6e907`。C 結題前讀過雙輸入比較原件 `gxa_01m8fe21mtypg6ms7n`。驗證器按完整 JSON 內容和已存 SHA 對回原件，再核對正式決定的引用。[A 正式研究記錄](parent/result.json) · [C 正式研究記錄](successor/result.json) · [移交與配方驗算](verification.json)

| 封存資料中的觀察 | 麥芽糖 | 異麥芽糖 |
|---|---:|---:|
| 由酵素分類連至 S288C 的天然蛋白 accession | 2 | 5 |
| 精確連至指定 Rhea 反應的該株 accession | 0 | 0 |
| 按 accession 聯集去重的 BRENDA 記錄 | 2 | 107 |
| 其中按催化底物欄位明載本題底物的記錄 | 0 | 11 |
| SABIO-RK 連至上述 accession 的記錄 | 0 | 0 |

兩題共用 0 個 accession，七個 accession 對應六種序列。異麥芽糖組 11 筆包含 10 筆單列 accession 與 1 筆共同列名。按化合物名稱直接搜尋會再得到三條 Ki 抑制記錄。這會高估支持催化底物的記錄數。C 因而採用分頁表述，兩頁保留各自的證據；反應的個別催化方向及受測菌株等缺口仍然列出。數值是封存檔案的去重筆數，食品菌株的活性仍須有相符實驗原件才能確認。[獨立來源與各組分母](../sources/reference.json) · [Lead 結論與條件限制](successor/DECISION.md)

C 在首次建圖歷經九次格式或分支資源拒收，事件 128 自行修正並生效。三個 CPU Jobs 均成功且各有獨立執行身份。保留原九次拒收及失敗欄位，後來的診斷改進以原請求另行回放。這次研究量測的是兩個底物的封存資料可複用性。[原拒收與事件](receipts.json) · [四筆原錯誤診斷對照](../../diagnostics/field.json)

從產品根目錄，可對交付回執和獨立資料參照直接重驗。成功時退出碼為零。

```bash
field=acceptance/science_assets/enzyme/field
.venv/bin/python -B "$field/verify.py" "$field/receipts.json"
```
