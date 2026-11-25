# 公開沉積座標研究的重播

此封存記錄候選 `82ee888158431c9031236b2e3642a50efcdbd1d0` 的 GX Run `gxr_01m8abxe10spc1eqmg`。官方 Codex 宿主連到本機腳本，GX 派發了四個真實 CPU Job。外部供應商呼叫與真模型科研決策均為零。

在該版本的倉庫根目錄，以鎖定環境重播原件、決策和具名輸出。

```bash
.venv/bin/python -B acceptance/v623/study/archive.py replay \
  --receipts acceptance/v623/evidence/study
```

重播會把唯讀備份解開到暫存目錄，核對 14 筆工具呼叫、7 次簡報投遞與決策及 4 筆主張提交，並從原始 mmCIF 以獨立解析和 SVD 複算兩份沉積的兩種遮罩。封存收錄了原始位元組、已結案 Run 的 SQLite、具名 Job 產物、方法與協議、正式投遞及本機受控會話記錄。[重播回執](replay.json) · [研究回執](study-receipt.json) · [獨立數值查核](../independent/result.json)

[逐鏈對與區段的資料表](tables/pair_intervals.csv) 有 228 列，保留未達區段資格的列。[逐沉積摘要](tables/deposit_summary.csv) 有 8 列，分別報告兩個端點。[資料表回執](tables/table-receipt.json) 提供產生每張表的具名輸出、原件雜湊、Job ID 和 CSV 雜湊。以下命令可直接由封存重建並驗證兩張表。

```bash
.venv/bin/python -B - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from acceptance.v623.study.archive import ARCHIVE, unpack
from acceptance.v623.study.tables import export

published = Path("acceptance/v623/evidence/study")
receipt = json.loads((published / "tables/table-receipt.json").read_bytes())
with TemporaryDirectory() as directory:
    base = Path(directory)
    unpack(published / ARCHIVE, base / "run")
    products = [base / "run" / row["archive_path"] for row in receipt["inputs"]]
    reconstructed = export(products, base / "tables")
    assert reconstructed == receipt["tables"]
    print(reconstructed)
PY
```
