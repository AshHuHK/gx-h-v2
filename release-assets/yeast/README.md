# 選用的 S288C 酵母資料及舊 MCP

核心套件包含 GX 插件、方法卡及樣本。此附件提供該案例另行讀取的兩個 SQLite **資料庫索引**、S288C BLAST 資料庫、BLASTp 2.17.0 及其動態函式庫、UniProt MCP 的已建置入口、Node 生產依賴、所用 Python 3.11 套件及來源告示。兩個 SQLite 檔保存版本化科研資料。

從本版 Release 下載 `gx-yeast-evidence-dependencies-linux-x86_64.tar.gz` 與同名 `.sha256`。在新目錄解壓。

```bash
sha256sum -c gx-yeast-evidence-dependencies-linux-x86_64.tar.gz.sha256
mkdir gx-yeast-deps
tar -xzf gx-yeast-evidence-dependencies-linux-x86_64.tar.gz -C gx-yeast-deps
(cd gx-yeast-deps && sha256sum --quiet -c SHA256SUMS)
```

原插件按版本綁定路徑。需要在全新部署使用它時，在容器或獨立部署目錄將下列來源映射到 [mounts.json](../../gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1/methods/mounts.json) 對應位置。既有共享部署可直接使用原掛載。

| 插件欄位 | 部署方提供的來源與目標 |
|---|---|
| enzyme_assets | 將附件的 `enzyme-assets/` 掛到設定中的同名資料根目錄。 |
| blastp | 將整個 `blast/` 掛到設定中含 bin/blastp 的環境根，讓原生動態載入器能找到同一份 lib/。 |
| mcp_runtime | 將附件的 `GX-harness/` 映射到設定中 integrations/macromnex 的上兩層。服務建置檔與單服務 registry 使用相對位置。 |
| mcp_python | 保留附件完整目錄，在設定中的 python 路徑建立指向附件 `mcp-python` 的符號連結。啟動器用系統 CPython 3.11 與附件內 python-site。 |
| mcp_node | 使用核心包附帶的 Node 22.17.0，將 bin/node 映射到設定中的對應位置。 |

離線檢查會使用兩個索引實際查詢、以封裝的 BLASTp 執行比對，再經本機 stdio 與封裝的 UniProt MCP 交握、列出操作。驗收命令在核心部署根目錄執行，第二個參數為附件解壓位置。

```bash
export PATH="$PWD/vendor/node/bin:$PATH"
depdir=/path/to/gx-yeast-deps
"$depdir/mcp-python" release-assets/yeast/verify.py "$PWD" "$depdir"
```

即時取得 UniProt 最新紀錄仍需連到官方服務。資料來源為 Rhea 142、Swiss-Prot 2026_03、ENZYME 28-Jan-2026。原始來源與授權在附件 NOTICE.md，程式版本在 blast/distributions.json、python-distributions.json 與兩份 Node lock。原研究的實際 MCP 查詢可在[酵母科研驗收](../../acceptance/science_capabilities/YEAST_REVIEW.md) 中追查。
