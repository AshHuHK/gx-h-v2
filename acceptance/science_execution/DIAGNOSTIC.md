# V6.2 公開收尾兩案

同一個 GX Goal 入口操作兩個原始分派。01 使用 P4，Lead 直接量測；02 使用 K7，Goal 明確要求 Lead 將盤點交給獨立模組，再自行選擇行動。兩案的原件、變換、方法卡、政策、模型和分派在登記時固定。這兩個診斷各自判讀。舊系列的四個分派、未結曝險和分母見 [HISTORY.md](HISTORY.md)。

## 本機預演

從候選 checkout 根目錄執行，下列兩個參數分別是尚不存在的外部輸出目錄，以及這台機器的 Clash 可執行檔。腳本在無外網路由的網路命名空間，使用正式 Codex App Server、本機 Responses 腳本、真正的 GX Job 與 SQLite。輸出含兩案首次執行、原 Run 復核、重啟與唯讀匯出。模擬用量及四則運算檢查只計入工程驗收。

```bash
bash acceptance/science_execution/rehearse.sh "$OUTPUT" "$CLASH_BINARY"
```

另外可用同一個命名空間工具驗證真正的程序中斷。在 GX 建立 Run 後、控制器落盤 Run ID 前強制退出，再以新程序執行原始 resume。

```bash
.venv/bin/python -B acceptance/phase2a/regression/loopback.py \
  --clash "$CLASH_BINARY" --receipt "$OUTSIDE_DIR/cold-network.json" -- \
  .venv/bin/python -B acceptance/science_execution/recovery_check.py \
  --work "$OUTSIDE_DIR/new-cold-run"
```

## 官方 OAuth

從同一個乾淨的候選 checkout 操作。將 `WORK` 指向外部私有的 cohort 目錄，將 `APPROVAL` 指向綁定該目錄、候選 SHA、後繼系列與帳戶的操作員授權檔。只走 Clash。預檢用官方帳戶、配額與模型目錄的讀取 RPC；這個命令沒有研究 turn。

```bash
diag() { .venv/bin/python -B acceptance/science_execution/diagnostic.py "$@" --host oauth; }
diag preflight
diag register --work "$WORK" --approval "$APPROVAL"
diag status --work "$WORK"
```

每案 900 秒開工、360 秒收尾、120 秒唯讀對帳。絕對時刻在第一筆原始嘗試落盤，同步寫入 GX Goal。限制作用於 GX 新 turn、steer 和 Job 准入，一次已發出的 turn 內可能還有多個底層請求。供應商未出具美元帳單時，實際金額記為未知。須先有明確批准才可執行下一行，這是第一個可能使用模型的命令。

```bash
diag run --work "$WORK" --slot 01 --approval "$APPROVAL"
diag status --work "$WORK" --slot 01
diag reconcile --work "$WORK" --slot 01
diag export --work "$WORK" --slot 01
```

第一案結束並完成用量對帳後才准入第二案。科學結果是否通過，分案保留。新的未決用量會保留原始帳；恢復始終使用相同的 Run、帳戶、時鐘、材料和原分派。

```bash
diag run --work "$WORK" --slot 02 --approval "$APPROVAL"
diag status --work "$WORK"
diag export --work "$WORK" > "$WORK/diagnostic-export.json"
```

停止一個在途案例會通知控制器，並在已有 Run 時直接執行本機 GX 暫停。原 turn、已入場作業及預留仍有回執可查；供應商端是否已取消，取決於官方記錄。暫停後可恢復同一案。已終態的 resume 只復核歷史。若過了收尾期限，resume 對仍在活動的原 Run 執行有界本機停止，再讀取原始帳；reconcile 永遠只讀研究狀態，更新匯出的帳務觀測。

```bash
diag stop --work "$WORK" --slot 01
diag resume --work "$WORK" --slot 01 --approval "$APPROVAL"
diag reconcile --work "$WORK" --slot 01
```

登記在 `registration.json`，原始嘗試與時鐘在各案的 `first-attempt.json`，操作命令及 GX 輸出在 `raw/commands.json`，具名 Job、來源與上層狀態在 `summary.json`。每案的 `accounting.json` 與 `role-accounting.json` 分別提供原始用量查核及依 Run／模組 scope 的彙整。OAuth 僅觀察到回應時，實體請求數保留未知。對帳不會補送模型請求。匯出只提供研究判讀所需資料；私有帳號資料留在既有官方宿主。

兩案都有正式讀數、首個處置提交、實際動作、用量終值及操作員對科研理由的覆核，才可凍結。人工核閱請從同一登記目錄以 `witness.py show` 讀原始證據，之後透過 `witness.py review --help` 所列參數登記判斷。首次失敗仍屬原分派。對公開診斷以外的第三沉積研究，使用獨立研究登記。

兩案的覆核回執完成後，在原目錄產生凍結證據。此命令從原始回執重新判讀兩案並核對帳戶、授權、首輪效果與理由覆核。

```bash
.venv/bin/python -B acceptance/science_execution/witness.py freeze --work "$WORK" --approval "$APPROVAL"
```
