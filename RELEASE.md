# Linux x86_64 發行與依賴

## 安裝核心

從 [本版附件](https://github.com/AshHuHK/gx-h-v2/releases/tag/science-capabilities-2026-11-27) 取得核心 tar 和同名 .sha256。系統需提供 Linux x86_64、glibc、CPython 3.11（驗收為 3.11.9，含 venv 與 pip）、tar、xz 及 sha256sum。以下命令在新的安裝目錄建立環境。

```bash
sha256sum -c gx-science-capabilities-linux-x86_64.tar.sha256
mkdir gx-science-release
tar -xf gx-science-capabilities-linux-x86_64.tar -C gx-science-release
cd gx-science-release
(cd release-assets/node && sha256sum -c SHA256SUMS)
mkdir -p vendor/node
tar -xJf release-assets/node/node-v22.17.0-linux-x64.tar.xz -C vendor/node --strip-components=1
export PATH="$PWD/vendor/node/bin:$PATH"
python3.11 -m venv .venv
.venv/bin/python -m pip --isolated --disable-pip-version-check install --no-index \
  --find-links release-assets/python --require-hashes -r release-assets/python/requirements.lock
.venv/bin/python -B scripts/release_assets.py all
.venv/bin/python -B bin/gx-p0 plugin list
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py --help
```

使用 Git checkout 單獨建置時，先從 [官方 Codex npm 發行檔](https://registry.npmjs.org/@openai/codex/-/codex-0.169.0-linux-x64.tgz) 下載到 `release-assets/codex-0.169.0-linux-x64.tgz`，並把 [Node 官方壓縮檔](https://nodejs.org/dist/v22.17.0/node-v22.17.0-linux-x64.tar.xz) 放到 `release-assets/node/node-v22.17.0-linux-x64.tar.xz`，再執行上列環境與安裝命令。工作區內下載須走本機 Clash。核心附件已含兩者，`release_assets.py` 會核對摘要。

| 內容 | 版本及位置 |
|---|---|
| Codex App Server、原始碼、LICENSE/NOTICE、實驗性 schema | 0.169.0。執行檔位於核心附件，來源和 schema 壓縮檔也在 Git。上游提交、下載位置及各層摘要見 [lock.json](release-assets/lock.json)。 |
| Node、npm、Cordis 執行環境 | Node 22.17.0、npm 10.9.2，使用官方 [Linux x64 發行檔](https://nodejs.org/dist/v22.17.0/)。Cordis 4.1.0 與三個傳遞套件的 tarball 隨包，版本見 [npm lock](gx-h/runtime/plugin-host/package-lock.json)。 |
| Python 科研運算與閱讀 | Biopython 1.88、NumPy 1.26.4、pypdf 7.2.0 的 wheels 和 [雜湊鎖](release-assets/python/requirements.lock)。 |
| 插件、材料及恢復 | [安裝目錄與版本摘要](gx-h/config/workflow_packages.json)、[研究範例](examples/)、座標、材料來源、舊版狀態遷移測試夾具。 |
| 酵母資料及工具 | 選用附件 `gx-yeast-evidence-dependencies-linux-x86_64.tar.gz`，包含其摘要檔與獨立掛載說明。只在使用舊酵母索引、BLASTp 或 UniProt MCP 路徑時需要。[掛載說明](release-assets/yeast/README.md) |
| 模型與網路 | 真模型使用執行端自己的官方 ChatGPT OAuth 登入。研究網路經本機 Clash HTTP 7890 / SOCKS5 7891。外部資料庫的即時查詢仍依賴對應服務。DSH SDK 屬另一種可選後端，其版本與安裝方式見 [SDK 說明](gx-h/runtime/dsh-sdk/README.md)。 |

## 啟動、重開與驗收

```bash
export PATH="$PWD/.venv/bin:$PWD/vendor/node/bin:$PATH"
gx_work=$(mktemp -d)
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$gx_work" research \
  --state "$gx_work/state" --run-key top7-coverage \
  --goal-file examples/deposited-structure-controls/top7.md \
  --goal-config examples/deposited-structure-controls/top7.json
```

保留工作目錄及 run-key。中斷後使用相同命令重開，或在同一 state 以 `research --state "$gx_work/state" --run-id "$gx_run"` 指定原 Run。離線驗收可執行下列命令。逐組結果包含相容性、多輪、整合、科研能力與邊界測試。

```bash
.venv/bin/python -B acceptance/science_capabilities/verify_release.py /tmp/gx-release-check
```

回退前停止寫入，保留 workspace、SQLite backup、artifacts、runs、threads、research 與已安裝插件。使用升級前的程式及其相配備份重開原 Run。新舊部署各自保留。還原實例及封存配方的重開回執見[遷移說明](acceptance/science_capabilities/MIGRATION.md)。
