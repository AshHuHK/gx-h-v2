# 天然乳蛋白預測與獨立評估

普通 Goal 的站點目錄提供 `natural-food-fold@1`。輸入是一份包含 accession 與成熟鏈序列的 JSON。插件比對版本化 `specimens.json` 的 UniProt 來源與序列摘要，支援兩種不同的天然乳蛋白。新計算使用站點已授權的 AlphaFold 3，保存實際程序啟動／終止、權重與程式摘要、輸入摘要、信心資料、預測 mmCIF 與原版輸出條款。輸出登記為 model_prediction。

兩次輸出一致時，GX 可以按相同作業身分接管。Linux 後端的 `scheduler_ref` 記錄 runner PID 與啟動 ticks。模型程序另在 `provider-events.jsonl` 留存自己的 PID 與啟動 ticks，可查證僅中斷 Driver 時實際仍在執行哪一次模型。

在本站有 GPU 權限的專用部署，使用既有唯讀 source／env／weights 掛載與既有 JobService 的資源、GPU 排隊和計費。

```bash
.venv/bin/python -B acceptance/harness_final/gpu/configure_site.py "$B_RELEASE" "$AF3_MOUNT"
"$B_RELEASE/.venv/bin/python" -B "$B_RELEASE/bin/gx-p0" plugin verify org.gx.natural-food-fold@1.0.0
.venv/bin/python -B -m unittest discover -s acceptance/harness_final/gpu -v
.venv/bin/python -B acceptance/harness_final/gpu/site_probe.py "$B_RELEASE"
```

`AF3_MOUNT` 內含 `env/bin/python`、`source/run_alphafold.py`、`weights/af3.bin.zst`。專用部署僅開啟 `natural_food_structure` GPU scope。帳戶、權重及完整站點路徑不隨發行物發布。目錄查詢可以在無推理環境的系統中完成，能否派工另依本地實際部署與資源判斷。

本次 B 的研究材料為已表徵的天然牛乳 α-乳白蛋白。研究席只收到問題與 UniProt 來源的成熟鏈。獨立保管者先比對成熟鏈與金標沉積原件、凍結對位和計分規則，模型完成前不在研究席可見範圍提供這些材料。Cα 全鏈位置的覆蓋率和 RMSD 是本次空間形狀比較的讀數；酶活性、臨床安全性和分子設計均不在此試驗內。[輸入與計分凍結](preregistration.json)
