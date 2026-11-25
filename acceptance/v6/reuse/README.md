# V6 重用原件

從 V6 產品根目錄執行。此次取回的材料放在產品旁的 `../reuse-sources/`。產品只攜帶取用清單和重現程式，科研 Lead 的可見範圍由該 Run 的材料授權決定。

```sh
python3 -B acceptance/v6/reuse/materialize.py verify ../reuse-sources
```

`manifest.json` 逐檔記下來源、位元組數及 SHA-256。第一次部署的選用材料有 27 個檔案，共 220,908,576 bytes。

| 本機檔案 | 取得與用途 |
|---|---|
| `upstream/2RE9.cif` | 從 RCSB 經 Clash 下載，507,971 bytes，與 GX 固定的 2RE9 修訂 1.4 完全一致。SHA-256 為 `8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b`。原始出處、固定判據與 CC0 聲明也在 `gx/runtime.tar`。 |
| `gx/native.bundle`、`gx/runtime.tar` | 從 v2 的不可變 Git 物件取得。前者可在離線機器還原 native commit `23d581c`，後者是 v2 commit `e02df99` 中選用的 P0 原碼、workflow、合約、插件、座標及測量程式。實查兩庫的協作模組與記憶模組有相同 Git tree。 |
| `gx/historical-work.tar` | P2A.1 已發行的兩組驗收原件、固定 CPU 方法回執、歷史蛋白 OAuth 的原始紀錄和固定網格收據。近期 V6 科研需用自己的 session、Job 與決策回執。 |
| `codex/`、`wheels/` | 官方 Codex 0.169.0 的原碼、生成 schema、LICENSE、NOTICE 和 Linux x64 npm 發行物，以及 GX release 固定的兩個審閱 wheels。此次自本地已下載的 release 檔取用。核驗 tar 中的 Codex binary SHA-256 與發行鎖一致；原始碼 tar 的 Git pax comment 指向 `d32063f9`。 |
| `kit/` | 使用者提供的 V5 及 V6 原始 ZIP。V6 的工程用固定方法回執位於 `protein/baseline/v6_run.json`，Run 為 `gxr_01m88424cctwgg1ryt`。另一本早期蛋白包所引用的 `new_run.json`（`ed431a3b…`）只在說明文件查到摘要。 |
| `upstream/{XScientist,CORAL,AutoScientists,AI-Scientist-v3,chaperone}/` | 由固定 commit 下載的 14 份原始碼、規格、設定與授權檔。以 Git tree 重建及 blob 雜湊驗證。XScientist 提供主張／證據規格，CORAL 與 AI-Scientist-v3 提供比較設計，AutoScientists 提供近噪聲結果與停止研究的參考；Chaperone 提供早期對稱性測量源碼。這些材料供工程與方法參照。 |

## 從空目錄重建

在保有 `e02df99` Git 物件的產品 checkout 操作。將原始兩個 ZIP 放在 `--kits` 指定的目錄。對外下載只使用本機 Clash HTTP 代理 `127.0.0.1:7890`。下載時如上游檔案變動，取用程式會報出不符的檔名。已知鎖定的 Codex npm 原檔可用 `--binary-archive /path/to/codex-0.169.0-linux-x64.tgz` 供應，免去再次下載。

```sh
python3 -B acceptance/v6/reuse/materialize.py fetch /tmp/gx-v6-cache --repo . --kits ..
python3 -B acceptance/v6/reuse/materialize.py verify /tmp/gx-v6-cache
git clone --branch migration-baseline /tmp/gx-v6-cache/gx/native.bundle /tmp/gx-native-23d581c
```

若 RCSB 日後更新原端點，`git show e02df99:gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif` 仍可取回指定修訂的位元組。重建時按清單核對 SHA-256。V6 適用的科學主張、原始計量與回復結果另見各 Run 的驗收紀錄。
