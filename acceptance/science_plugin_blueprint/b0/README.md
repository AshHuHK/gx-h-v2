# Top7 公開歸檔校準

本例從普通 Goal 匯入三份材料，問題沒有方法名或預設答案。原件為公開 1QYS 的 mmCIF、已存檔的固定片段預測輸入，以及 92 對聚合物位置的顯式映射。其 SHA-256 列在 [goal.json](goal.json)。PDB 的原始下載時間、URL、修訂與用途登記於 [來源記錄](../../science_capabilities/structure_controls/sources.json)。AF3 的參數和輸出適用部署方原有的授權與輸出條款。每個成功的預測 Job 保留原版 TERMS_OF_USE.md。

## 本地部署與運行

以獨立安裝目錄運行下列命令。該目錄有完整產品、既有官方 Codex OAuth 帳戶和 Clash。共用的 AF3 代碼、環境和參數由該部署的操作員提供，保持唯讀。參數檔名、大小和 SHA-256 需符合插件 manifest。以下部署命令會把本安裝的 GPU scope 設為 archived calibration，單項 GPU 需求 16 GiB，仍須保留現有 GPU 最低剩餘容量和同時運行上限。

```bash
b=acceptance/science_plugin_blueprint/b0
.venv/bin/python "$b/prepare_deployment.py" --deployment "$PWD" \
  --env "$AF3_ENV" --source "$AF3_SOURCE" --weights "$AF3_WEIGHTS"
.venv/bin/python -B bin/gx-p0 plugin verify org.gx.archival-fold-calibration@1.0.0
export GX_P0_CODEX="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
gx=(.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$B0_PRIVATE/host")
"${gx[@]}" start --state "$B0_PRIVATE/state" --run-key B0-archived-top7 \
  --goal "$(cat "$b/question.md")" --goal-config "$b/goal.json" > "$B0_PRIVATE/start.json"
read -r rid ref < <(.venv/bin/python -c 'import json,sys; x=json.load(open(sys.argv[1])); print(x["run_id"],x["materials"][1])' "$B0_PRIVATE/start.json")
"${gx[@]}" observe --state "$B0_PRIVATE/state" --run-id "$rid" --artifact-ref "$ref" \
  --sha256 d66b95ad11410ee8ff8b407abc3ea89c5401825170c9cc519556a382334c81bd \
  --source 'wwPDB:1QYS@1.3' --acquisition "$b/reference-acquisition.json"
"${gx[@]}" research --state "$B0_PRIVATE/state" --run-id "$rid" --timeout 1000
.venv/bin/python -B "$b/verify_measurement.py" "$B0_PRIVATE/state" "$rid"
```

`B0_PRIVATE` 是預先建立的私有工作目錄。冷恢復重用同一條 `research --run-id` 命令和原目錄。插件包可藉 `bin/gx-p0 plugin digest` 與 `plugin install … --sha256 …` 安裝至其他相容部署。既有 Goal 內容釘選保留原版本。

## 度量及邊界

模型 Job 只接受校準輸入的確切位元組，執行目前掛載且摘要匹配的 AF3。CPU 方法讀取兩個 mmCIF 和完整一對一映射，使用 model 1 的 Cα 坐標，以 proper rotation/Kabsch 疊合。缺失位置留在分母，另列哪份坐標缺失。歧義或座標不足會終止這筆量測。獨立驗證從另一份 PDB 文字讀取實驗坐標，將新 Job 的預測以四元數 QCP 重算。92 對的參考 RMSD 為 1.011876 Å，部署重現容差為 ±0.10 Å，兩種重算相差容許 0.000001 Å。[事前判據](preregistration.json)

```bash
.venv/bin/python -B -m unittest discover -s acceptance/science_plugin_blueprint/b0
```

這是早於模型訓練時期的單一已知沉積。QCP/SVD 和不同座標檔格式驗證數值計算；資料都來自 1QYS，樣本數仍為一。

## 這次正式回執

Run `gxr_01m8f5r59yyx9gmekh` 的 Lead 用 1 個 GPU Job 和 1 個 CPU Job 得到 92/92 個可比 Cα，疊合 RMSD 為 1.0089501331 Å。獨立 PDB/QCP 得 1.0089501331 Å，兩者相差 1.29 × 10⁻¹⁴ Å，與歷史參考相差 0.002926 Å。推論耗時 216.27 秒。GX driver 在推論時實際被終止，新的 driver 恢復時保有同一 Job 和同一 runner。CPU Job 的正式三項讀數在事件 206 送入簡報，Lead 在 208 直接讀取產物，219 提交生效，221 結題。科學 Goal 為 CLOSED，Run 為 STOPPED，兩個 Job 均為 SUCCEEDED。[測量](measurement.json) · [恢復](recovery.json) · [讀數送達](delivery.json) · [結論與範圍](REPORT.md) · [具體決定](DECISION.md)

原始部署的離線樣板把含訂閱的模型回合也設成零。首次使用現有 OAuth 配額的模型探查發現附件欄位不合格式。兩者保存在 [原始失敗](first_failures.json)。正式 Run 的 GPU 批准曾在開工中補登，PI 透過普通消息把既有批准狀態回覆給 Lead。新的部署腳本在開工前設好該 scope。Lead 在科學結題前沒有保存應 PI 要求的配方；後繼保存紀錄需註明實際作者。完整輸出區分本次預測模型座標以及事前可檢驗的科學預測登記，原來的簡報和第一版輸出可追溯。[首次原報告](REPORT.initial.md) · [結題事件](closure.json) · [授權及算力環境](environment.json)
