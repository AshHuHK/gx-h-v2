# GX-H V6.2.1 工程簽收與 V6.2C 公開試點

候選提交為 `83e8f34d2886a5c4cedc966ec4a7dd7b2addf691`。簽收以這個提交的乾淨 checkout 啟動。發行分支 `v6.2.1-evidence` 的後續提交封存文件和回執。[逐項索引](acceptance/v621/receipts/index.md) · [機讀索引與實際命令](acceptance/v621/receipts/index.json)

| 決定 | 實測範圍 |
|---|---|
| V6.2.1 工程修補可簽收 | 實際原件、具名輸出、交付與發布的鏈條，以及逐項缺口、目標改版和條件派發，均已經過正常 Goal 入口驗證。 |
| 四格離線排演已完成 | 4/4 形成已送達且可判讀的新證據，4/4 執行相應操作。可委派臂 2/2 有完整子模組鏈。 |
| 公開真模型待授權 | 公開 0/4、原未見配對 0/2。公開批次另需能覆蓋在途請求的可執行計費上限。 |

## 可親自簽收的一條證據鏈

Run `gxr_01m89nah7xra87mf0m` 使用操作員登記的 507,971 bytes 2RE9 mmCIF。事件 22 登記原件，事件 51 的 CPU Job admission 指向相同 artifact_ref 與 SHA。具名 `geometry.json` 將此遮罩的分子與分母記為 49 和 164。獨立 Biopython 解析原件重算，整數差和比值絕對差皆為 0。

事件 74 發布模組報告。官方 Codex App Server 的第 8 次本機請求實際將同一個 `49/164 (0.299)` 送給 Lead，事件 86 採納正式主張。發布值為 `open / undetermined`。冷恢復和兩次報告重送均使用原回執。把這份報告或主張寫成 149/164，或以另一個 Run 內具有相同 SHA 的原件冒作授權材料，均被拒絕。[原件至正式結論、獨立複算和兩條拒收鏈](acceptance/v621/receipts/coordinate-publication.json)

對實測方向有判別力的另一個回歸中，同一命題已被判為 `tested/opposed`，Lead 仍建議 `limited_support`。GX 保存提案與異議，向 API、下一輪簡報及正式匯出發布同一修訂 `contested`。已登記的數字只解答匹配該材料、端點和遮罩的缺口；驗收另有一份讀數兩個欄位分別回答兩個缺口的正例。[反證主張](acceptance/v621/receipts/claim-receipts/opposed.json) · [座標缺口](acceptance/v621/receipts/gap-acquired-analysis.json) · [兩項匹配](acceptance/v621/receipts/gap-two-matches.json)

## 公開試點的四個腳本 Run

| 格 | 架構 | 介入後實測 | Lead 引用後的實際行動 |
|---|---|---:|---|
| 01 | 單層 | 149/164 | 維持原分支，完成參考檔共同骨架比較 |
| 02 | 可委派 | 54/164 | 停止原分支，完成較窄的參考檔 Cα 比較 |
| 03 | 單層 | 54/164 | 停止原分支，完成較窄的參考檔 Cα 比較 |
| 04 | 可委派 | 149/164 | 維持原分支，完成參考檔共同骨架比較 |

兩臂均為 `deep`，讀取相同方法卡、使用相同量測。四個 Run 共完成 8 個 CPU Job，8 份正式主張各有自己的材料和讀數，均維持 `open / undetermined`。每個單層格 12 次原生工具呼叫，可委派格 19 次。可委派格在兩組配對分別多耗 0.110 秒和 1.394 秒。腳本、模型判斷及真人盲評各有不同證據要求；本輪實測限於腳本，付費模型成本比尚未觀測。[逐格判分與盲法限制](acceptance/v62c/receipts/score-review.md) · [單層與委派能力差異](acceptance/v62c/receipts/capability-parity.json) · [付費和私有研究狀態](acceptance/v62c/receipts)

在 repo 根目錄可獨立驗索引、重跑完整排演，或從發行包重算已發布的四份評分。以下示例使用已安裝的鎖定依賴。

```bash
.venv/bin/python -B acceptance/v621/index.py verify --require-complete
# 先把 GX_CLASH 設為本機 Clash 可執行檔路徑。
batch_dir=$(mktemp -d)
bash acceptance/v62c/run-public.sh "$batch_dir/public" "$GX_CLASH"
replay_dir=$(mktemp -d)
tar -xzf acceptance/v62c/receipts/public-raw-replay.tar.gz -C "$replay_dir"
.venv/bin/python -B acceptance/v62c/rate.py --work "$replay_dir"
cmp "$replay_dir/scores.json" acceptance/v62c/receipts/scores.json
```

可攜證據包包含原始公開材料、四份 SQLite 快照和宿主的實際會話紀錄。簽收時重算結果與原評分逐位元相同；一次額外重放攔截了對原驗收工作目錄及舊版安裝的讀取，從解壓目錄開啟四份資料庫。[可攜包重驗回執](acceptance/v62c/receipts/portable-replay.json) · [讀取路徑複核](acceptance/v62c/receipts/portable-isolation-proof.json)

## 來源與判讀

審閱基準為 `321b3b9542e986e749b4eab08495b288d09ba4be`。實施包 SHA-256 為 `4036d9dc6bc16b10e2d902dbc55b3b5ee1d1917463c39c26aba05a3f42ea2cf6`。先在舊版重現同一命題已受到反證卻發布支持、無關讀數關閉多個缺口兩項錯誤。紅燈測試與舊版輸出固定於 `7157b6b`，共同判定的第一個修補為 `25cbfa2`。[舊版失敗收據](acceptance/v621/receipts/baseline) · [發布判定表](acceptance/v621/CLAIM_PUBLICATION.md)

對新 Goal，操作員的原件取得紀錄、實際 Job 的輸入與輸出 SHA、Lead 收到的報告以及正式發布值都有獨立引用。2RE9 的 `ordered_backbone` 量測為 49/164，顯示 0.299。這描述特定模型、鏈與原子選擇下的覆蓋率。2RE9 與 2O0O 的材料紀錄保留共同發表來源，採樣獨立性未知。[方法及資料來源](examples/v621-coordinate-analysis/README.md)

公開試點使用另一個前瞻固定的遮罩 `common_backbone`。兩個公開材料是同一沉積原件的受控派生檔，門檻為 3/5。兩臂均需要從所分派檔案取得讀數。一個材料上的 149/164 可有真實根據；把它寫入另一個實際只有 49/164 的輸出會被拒絕。[公開協議](acceptance/v62c/protocol.json) · [方法卡](acceptance/v62c/method-card.json)

## 啟動與回退

沿用 V6.2 的 Python、Node、Codex 和 Cordis 鎖定依賴。啟動點仍為 `bin/gx-p0 start`。建立新版研究時可在 Goal grant 指定以下欄位，並根據研究設定 `input_files` 和預算。單層用空陣列，可委派臂用 `["data_inventory@2"]`。[安裝與 Goal 配置](V62B_DEPLOYMENT.md)

```json
{
  "evidence_policy": "gx.evidence/6.2.1",
  "research_effort": "deep",
  "research_modules": [],
  "branch_delegation": false,
  "plugins": ["org.gx.protein@0.3.0"],
  "capabilities": ["deposited-chain-geometry@2"]
}
```

先建立 Goal，從材料紀錄取得原始 artifact_ref 與 SHA，經 `observe` 登記取得紀錄後再執行 Job。首個 Lead 全域決策後到達的材料走 `assign-material`，該命令綁定決策、原件和可讀範圍。Run 續跑使用相同 state 及 run_id。[登記原件範例](examples/v621-coordinate-analysis/README.md)

升級前停止正在寫入該 state 的 Driver，保存整個 state、SQLite 與 artifacts。以候選版開啟原路徑；既有 Run 保留原政策、插件、報告、原始用量與輸出雜湊。需要回退時保留升級後 state，還原完整升級前副本，然後用原版程式開啟。舊版本和各階段收據使用分開的副本。[同路徑升級及實際回退程序](acceptance/v621/compat/README.md)

新增證據規則屬於 Goal、研究模組、主張及評估的現有 owner。派發接點在節點准入前核對目前條件，對固定版本確定不符的條件只結算該路徑。核准、派發、工單恢復、執行鍵與帳務沿用原有 JobService。跨層完整解耦留待 V6.3。

## 測試範圍與恢復

在凍結提交上，既有測試共發現 645 項，641 通過、4 跳過、0 失敗。4 項是未安裝選配 DSH Python SDK/runtime 的測試。另執行 V6 工程驗收 13/13、V6.2A 的 A1–A7、V6.2B 的四個歷史工程組。歷史組在建立 Goal 時明示原政策，逐個核對儲存的版本；新試點在新版政策下驗收。[完整回歸與跳過紀錄](acceptance/v621/receipts/compat/summary.json) · [驗收命令與日誌](acceptance/v621/receipts/index.md)

本次修補套件 37/37，因果計分及付費邊界 11/11，文獻引文與數值來源的聚焦回歸 16/16。這些組別有重疊。真正送達的文獻原句 `3.000 angstroms` 可按來源引用；改成 4.000、冒稱 GX 實測或夾帶另一筆 149/164，均會被拒絕。原件的歷史 `deposited-chain-geometry@1` 在升級前後各重跑一筆 CPU Job，舊 nature 仍是 `unclassified`，兩份輸出逐位元相同。[修補日誌](acceptance/v621/receipts/commands/repairs.log) · [引文正反例](acceptance/v621/receipts/commands/quotations.log) · [歷史輸出比較](acceptance/v621/receipts/legacy-coordinates/comparison.json)

原版 Run 經兩次冷啟、兩次報告重送後，Job、turn 與費用均無新增。另一個仍在進行的舊 Run 接受新觀察後，成熟度游標由 16 前進至 18。還原原版程式與完整備份後，游標回到 16，原結論與帳本雜湊一致。[升級前](acceptance/v621/receipts/upgrade/before.json) · [候選版重播](acceptance/v621/receipts/upgrade/check.json) · [回退結果](acceptance/v621/receipts/upgrade/restored.json)

首輪候選 `288661c` 的四格結果另外封存。該版完整回歸抓到合法數值引文與方法版本被誤擋，驗證器亦曾假設拒收案例的數量固定。修正後在本次候選重新執行四格、完整回歸及證據鏈。兩次乾跑分開計數。[前一候選紀錄](acceptance/v621/receipts/prior-candidate.json)

## 付費與後續版本

公開四格每格 100 萬、總計 400 萬 token 是擬議上限。既有准入能在預留滿時擋下新 turn；供應商單一已發請求的最終用量仍可超過預留。付費預檢在本機模擬兩種架構，最後一筆各預留 32,768、實報 32,968，結算一次後各為 1,000,200。三格剛好耗盡時的批次算例為 4,000,200，未知的最大曝險尚無上界。本次公開付費門檻還需要逐次可證實的最大用量、涵蓋 Lead 與子模組的共同預留，以及此批次的明確授權。[預檢程式](acceptance/v62c/spending.py)

公開乾跑、公開真模型與私有配對各自計數。原私有協議 `V62B-UNSEEN-PAIR-01` 鎖定舊執行基底和計分，應改、應維持各 0/1。新量表與曝光材料只適用於新的公開系列。若要評估未見配對，保管者必須先為新基底及新計分規則登記後繼協議，並保留原分母。待公開模型實際留下證據、決策及至少一條子模型量測至 Lead 行動的完整鏈，再判斷是否進私有實驗和 V6.3。四個格子不能估計行為可靠度。[舊協議與原始收據](acceptance/v62b/study/README.md)
