# GX-H V6 部署與恢復

PI 設定研究問題、原始材料、可用方法與支出上限。Scientific Lead 在官方 Codex 會話中決定要檢驗的解釋、派哪些工作、怎麼回應證據及何時停止。GX 在專案自己的 SQLite 庫保存 Goal、決策簡報、圖版本、工作身份、結果、主張和費用。產品和實際觀測的簽收範圍見 [V6 驗收報告](V6_ACCEPTANCE.md)。

## 從現有部署啟動

在本機的 `gx-v-6/product` 執行。示例授權固定版本的 2RE9 mmCIF 和三個方法，並給出 5 個 Job、300 CPU 秒、5 個模型 turn 及 100 萬 token 的准入上限。研究路線和判定由 Lead 決定。第一次 `start` 建立 Goal、不可變材料、帳戶與首個決策任務。付費推理從 `run` 開始。

```bash
source .venv/bin/activate
bin/gx-p0 plugin verify org.gx.protein@0.1.0-v6.1
bin/gx-p0 login-status
state_dir="$PWD/var/my-2re9-study"
bin/gx-p0 start --state "$state_dir" --run-key my-2re9-study \
  --goal "$(cat examples/v6-2re9/goal.md)" --goal-config examples/v6-2re9/grant.json
bin/gx-p0 status --state "$state_dir"
bin/gx-p0 run --state "$state_dir" --timeout 600
```

既有 OAuth 登入由官方 Codex 管理。新機可先執行 `bin/gx-p0 login` 完成設備登入，再以 `bin/gx-p0 doctor` 確認帳戶、模型及推理檔位。預設 Lead 用 `ultra`，本次驗收用 `xhigh`。Linux 版宿主的 HTTP、HTTPS、WS 和 WSS 走設定中的 Clash HTTP 代理。宿主在兩個協定的有效代理均可連線後啟動。此部署保留 `ALL_PROXY=socks5h://127.0.0.1:7891` 與僅環回可繞行的設定。[模型檔位](MODEL_PROFILES.md)

需要改變研究輸入時，可另建 grant JSON。檔案路徑相對於 grant 所在目錄，`sha256` 可固定原始位元組，方法以版本化 capability ID 列出。相同 run-key 及輸入可讀回同一研究。狀態中 `workflow.id` 與 `graph_head` 在開題時都為空，第一份 `amend` 被採納後才有圖版本。PI 可從原有 `message` 或 `operator` 入口追加來源及變更正式需求。

本機已啟用 TCP `127.0.0.1:7891` 的 Clash SOCKS5 監聽，經既有環回 HTTP 代理 7890 轉送。從工作樹可用 `../network/listener status` 查詢；主機重啟後執行 `../network/listener start`，回退此監聽用 `../network/listener stop`。

## 停止、接手與讀帳

```bash
bin/gx-p0 stop --state "$state_dir"
bin/gx-p0 status --state "$state_dir"
bin/gx-p0 resume --state "$state_dir" --timeout 600
```

同一目錄有多個研究時加上 `--run-id`。工作階段逾時可使用同一目錄再次驅動。要在沒有 Codex 連線的情況下對帳既有工作，可執行 `GX_CODEX_OFFLINE=1 bin/gx-p0 run --state "$state_dir" --timeout 10`。狀態輸出包含圖頭、決策與節點，費用在 `resources`，原始逐 turn 回執在 `metering`。CPU 計量與預算進位各有欄位；provider 只報 token 時，USD 金額顯示 `null`。單一 turn 可能超過預留，未取得完整回執的用量會保留其估算或未決身份。Lead 採納 `close_run` 後，GX 等待已准入的工作對帳，再留下科學結題事件。

## 安裝到乾淨的 Linux x86_64 工作樹

本機與發行鎖使用 CPython 3.11.9、Node 22.17.0 與 npm 10.9.2。以下命令從本次交付的原始碼、發行鎖和離線包安裝。正式發行 tar 內會包含 Codex 的平台 archive，可直接執行 `python scripts/release_assets.py all`。獨立 checkout 可按下例使用下載目錄內的官方 archive。[資產與來源雜湊](acceptance/v6/reuse/README.md)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --require-hashes \
  --find-links release-assets/python -r release-assets/python/requirements.lock
python -B scripts/release_assets.py all \
  --codex-archive ../reuse-sources/codex/codex-0.169.0-linux-x64.tgz
bin/gx-p0 plugin verify org.gx.protein@0.1.0-v6.1
```

額外下載的外部設計原件、舊案例、檢查器和 reviewer 材料保存在研究會話之外。給 Lead 的示例輸入只有問題、grant 內的 mmCIF、公開方法說明、工作狀態和實際預算。[下載與離線重建](acceptance/v6/reuse/README.md)

## 舊專案與回退

V6 以增量 SQL 在現有狀態庫加入決策關係，舊 workflow 按原科學配方及 pin 恢復。升級有在途工作的專案前，暫停新的准入並保存一致的整份 state 快照，包括 SQLite/WAL、工作目錄、產物與相配的 Codex thread store。保留原 checkout 和配置，以便還原相同的 state 路徑及原版本。舊版回退使用該版的快照；已採納動態圖的專案需要保留 V6，讓原 JobService 繼續對帳，其歷史提交與費用仍可查。此次以獨立 V6 工作樹部署，P2A.1 基準標籤為 `v3.2-p2a.1`。
