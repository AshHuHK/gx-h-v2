# 公開四格的訂閱試點

系列 `GX-V62C-PUBLIC-COVERAGE-OAUTH-01` 使用原始四個公開分派，順序為 P4 單層、K7 可委派、K7 單層、P4 可委派。研究與盲評直接透過官方 Codex App Server 使用同一個已登入的 ChatGPT 訂閱。機器的外部通訊經 Clash。兩臂使用相同的 deep 研究政策、模型、方法卡與座標材料。可委派臂由 Lead 決定是否開啟資料模組。

PI 已授權使用現有包含額度，追加貨幣支出授權為 0。這次試點沒有 token 上限。GX 照常登記逐 turn 預留、確認用量及歸屬。帳戶快照可核對訂閱准入，供應商美元金額須有獨立帳單才能確認。額外付費要求、帳戶改變或原格回執尚未核對時，保留原 Run 和既有工作。每格的獨立盲評先鎖定首次證據機會，再檢查理由和實際行動。主張不清或裁決意見相左時保留待審。

## 啟動和續跑

從凍結的部署 checkout 執行。鎖定版本的 Codex binary 和 Python 環境沿用倉庫的部署說明。只讀預檢檢查官方帳戶、模型及包含額度。它不送出推理請求。

```bash
pilot=(.venv/bin/python -B acceptance/v623/oauth/pilot.py)
"${pilot[@]}" preflight
```

工作目錄與候選提交在同意回執中綁定一次。操作員從本機原始授權對話驗證 PI 回覆，可用 authorization.issue 取得同一份回執的路徑及 SHA。憑證由官方宿主讀取。下列命令中的 WORK、APPROVAL 和 APPROVAL_SHA 分別為該次登記的外部工作目錄、回執路徑及內容摘要。

```bash
admission=(--work "$WORK" --approval "$APPROVAL" --approval-sha "$APPROVAL_SHA")
"${pilot[@]}" register "${admission[@]}"
"${pilot[@]}" run "${admission[@]}" --slot 01
"${pilot[@]}" status --work "$WORK"
```

依序發起 02、03、04。後一格啟動前會重查前格的研究、盲評及用量回執。操作員可停止在途的 GX 工作，之後在同一目錄恢復。

```bash
"${pilot[@]}" stop --work "$WORK" --slot 01
"${pilot[@]}" reconcile "${admission[@]}" --slot 01
"${pilot[@]}" resume "${admission[@]}" --slot 01
```

原始 Run、決策及正式投遞保存在各格的 state、codex/sessions 和 judging 下。公開報告只摘錄核驗所需的數值、判斷、用量和回執摘要。本次試點、要求實體請求硬上限的正式系列、未見配對及兩份公開原件的 CPU 研究各有自己的分母。[科學分派與宿主規格](../LIVE.md) · [CPU 研究的執行與恢復](../study/README.md)

## 此次登記的結果

四個原格均已登記。01 的 Lead 實際讀取 149/164，時限內沒有提交針對該結果的後續行動。原帳本保留最後一個中斷 turn 的預留。研究中可逐筆核對的供應商回執合計至少 578,596 token；訂閱帳戶查詢沒有最後回應的正式結算。依登記的支出及對帳條件，01 的獨立盲評與 02–04 仍待入場。[四格結果](receipts/first-attempts/index.json) · [第一格原始回執核對](receipts/interrupted-turn.json)

要在原部署確認新的晚到證據，可針對同一格執行下列讀取。腳本複製所需的本機狀態，在暫存副本執行 GX 對帳，並對官方 App Server 做唯讀查詢。正式執行與使用者對話紀錄均保留於原工作目錄及既有宿主。

```bash
.venv/bin/python -B acceptance/v623/oauth/audit_tail.py \
  --slot "$WORK/01" --out /tmp/gx-oauth-01-usage.json
```
