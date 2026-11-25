# GX-H V6.1 啟動、升級與回退

V6.1 讓一份 Goal 使用操作員選定的插件方法。研究會話只取得該 Run 的材料、能力、額度及其實際觀測。靜態 workflow 保持原有入口。下列命令在產品根目錄執行。

## 新研究

本機專案目錄為 `gx-v-6/product-v6.1`。本部署使用固定版 CPython 3.11.9、Node 22.17.0、Codex 0.169.0 及 Cordis 4.1.0。從乾淨 checkout 安裝時使用隨版本固定的 Python wheels、Codex archive 和 Node 套件。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --require-hashes --find-links release-assets/python \
  -r release-assets/python/requirements.lock
python -B scripts/release_assets.py all \
  --codex-archive ../reuse-sources/codex/codex-0.169.0-linux-x64.tgz
bin/gx-p0 plugin verify org.gx.protein@0.2.0
```

解開本版 Linux 發行 TAR 後，Codex archive 已放在 `release-assets/`。此時直接執行 `python -B scripts/release_assets.py all` 即可；上例的 `--codex-archive` 指向本機原始碼 checkout 相鄰的固定發行物。

感測器範例使用兩個時段的讀值與參考值，公開配置只授權一種分析方法。將範例插件按固定摘要安裝到目前的部署目錄，再以公開 Goal CLI 啟動。

```bash
pin=$(python -c 'import json; print(json.load(open("examples/v61-sensor-calibration/package-pin.json"))["sha256"])')
bin/gx-p0 plugin install examples/plugins/sensor_calibration --sha256 "$pin"
bin/gx-p0 plugin verify org.example.gx.sensor-calibration@1.0.0
state_dir="$PWD/var/sensor-study"
bin/gx-p0 start --state "$state_dir" --run-key sensor-study \
  --goal "$(cat examples/v61-sensor-calibration/goal.md)" \
  --goal-config examples/v61-sensor-calibration/grant.json
bin/gx-p0 status --state "$state_dir"
```

此時圖頭為空，首個 Lead 任務已保存。一般研究的模型推理從 `bin/gx-p0 run --state "$state_dir" --timeout 600` 開始。感測器樣例允許一個 CPU Job 與兩次原生呼叫；另做真實模型研究時，可依研究額度調整 grant。已完成的結論、量測和費用可從同一個 state 查閱。

- 以 `bin/gx-p0 stop --state "$state_dir"` 暫停。以 `bin/gx-p0 resume --state "$state_dir" --timeout 600` 繼續。要在模型離線時只驅動已授權的工作，使用 `GX_CODEX_OFFLINE=1`。若唯一的待辦需要模型，限時到期可返回 75。
- Goal 的 `capabilities` 按能力 ID 與版本授權。空清單可讓 Lead 在沒有可派方法時開題。可用 `plugins` 指定包版本以消除多個提供者的歧義。精確 package digest 與方法 pin 寫入 Run，冷重開沿用該記錄。範例的蛋白重評工具只在相應授權下出現。
- `run_budget.job_submissions` 控制整個研究的 Job 准入。可選的 `initial_job_limit` 只限制空圖首次採納能新增的 Job 節點數。後續修訂仍受 Run 與分支剩餘額度約束。完整兩沉積結構驗收採用 10 次總提交、首圖最多 5 個工作。
- 既有 OAuth 仍由官方 Codex 管理。可用 `bin/gx-p0 login-status` 或 `bin/gx-p0 doctor` 查詢。本機保持 Clash HTTP `127.0.0.1:7890`、SOCKS5 `127.0.0.1:7891` 及僅環回繞行。

## 在原部署升級

1. 記下 V6 程式提交、實際部署與 state 的實體路徑、原 Codex thread store、選用的插件版本和配置。用舊 CLI 停止新准入，等正在寫入的 Job 與計量回執被保存。保管整份 state（包含工作目錄、產物、SQLite/WAL 與原狀態標記）、相配的原生會話存放區與舊版程式。各寫入端靜止後取得同一時間點的檔案系統快照或完整私有備份。
2. 在相同實體部署路徑切換到 V6.1，保留原 state 的實體路徑。原生狀態標記記錄了這兩個路徑。另建目錄的副本演練可用下方 migration probe；它會在獨立目錄用舊 CLI 建正確標記，再更換程式。
3. 先執行 `bin/gx-p0 status --state "$state_dir"`；原合同／DDL 已知配對由現有遷移所有者升級。用 `GX_CODEX_OFFLINE=1 bin/gx-p0 resume --state "$state_dir" --timeout 10` 對帳現有工作。在獨立程序再次查 status，比對圖頭、主張、Job／artifact ID、預留與結算。對在途 Job 先核對原 Runner 與 operation key 再決定是否需要新工作。
4. 回退時先保留升級期間新增的結果和費用，再將完整舊備份與相配的舊程式放回原位置。用舊 CLI 核對圖頭與帳本。V6.1 期間已執行的外部工作和支出仍是真實發生過的，恢復舊快照後須先對帳，避免重複派發。

下例在新的私有輸出目錄測試升級與回退。已發布 V6 的 checkout 為相鄰的 `../product`。命令操作測試副本。

```bash
python -B acceptance/v61/migration.py --out "$PWD/var/migration-check-01" --baseline ../product
```

合同身份、升級順序與保留的研究語義見 [V6.1 合同 ADR](gx-h/docs/v6.1-contract.md)。歷史 V6 的真實研究、首次中性對照失敗及兩種檢查器的差異，保存在[原始驗收報告](V6_ACCEPTANCE.md)。
