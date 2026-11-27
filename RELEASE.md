# 從原始碼啟動 GX-H

GX 使用部署機已配置的 Codex，由 App Server 的 stdio 介面建立會話。本版驗證的協定版本是 `codex-cli 0.169.0`。預設執行 PATH 上的 `codex`，安裝在其他位置時可設定 `GX_P0_CODEX=/absolute/path/to/codex`。登入使用執行端現有的 ChatGPT OAuth。

## 安裝 GX 依賴

使用 Python 3.11 與 Node 22.17 以上。本版驗收使用 Node 22.17.0 和 npm 10.9.2。在原始碼根目錄執行下列命令。若使用已配置好的 GX 環境，可直接執行下一節的檢查。

```bash
export HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890
export ALL_PROXY=socks5h://127.0.0.1:7891 NO_PROXY=localhost,127.0.0.1,::1
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" all_proxy="$ALL_PROXY" no_proxy="$NO_PROXY"

python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix gx-h/runtime/plugin-host --ignore-scripts --no-audit --no-fund
```

Python 版本在 [requirements.txt](requirements.txt) 與其引用的科研依賴檔。Cordis 由 [package-lock.json](gx-h/runtime/plugin-host/package-lock.json) 固定。已安裝的插件版本及內容摘要見[插件登錄表](gx-h/config/workflow_packages.json)。

## 連線和執行

```bash
bin/gx-p0 login-status
bin/gx-p0 plugin list
bin/gx-p0 doctor --role main
```

`login-status` 檢查登入方式。`doctor` 可驗證 App Server 版本、帳戶及模型目錄。下列命令透過同一宿主開啟一個實際研究 Run。需要專屬會話目錄時，自帶的 OAuth 入口也接受相同的 `GX_P0_CODEX`。[研究入口與範例](acceptance/science_capabilities/README.md)

```bash
gx_work=$(mktemp -d)
bin/gx-p0 research --state "$gx_work/state" --run-key top7-coverage \
  --goal-file examples/deposited-structure-controls/top7.md \
  --goal-config examples/deposited-structure-controls/top7.json
```

繼續研究時沿用同一個 state 和 run-key，或指定已存在的 Run ID。選用酵母案例透過插件讀取部署方提供的資料索引、BLASTp 和 UniProt MCP。對應欄位見[方法的掛載設定](gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1/methods/mounts.json)。既有封存 Run 的恢復程序及證據見[遷移說明](acceptance/science_capabilities/MIGRATION.md)。

## 重跑驗收

在已啟用的環境執行。測試覆蓋相容性、多輪會話、整合、科研能力及其邊界。

```bash
python -B acceptance/science_capabilities/verify_release.py "$(mktemp -d)"
```
