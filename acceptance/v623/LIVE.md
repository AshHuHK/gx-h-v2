# V6.2C 公開四格的正式入場

控制端有兩個獨立系列。GX-V62C-PUBLIC-COVERAGE-LIVE-01 使用可證實體請求上限的部署及外部授權。GX-V62C-PUBLIC-COVERAGE-LIVE-01-REHEARSAL 使用本機 Responses 替身，讓已安裝的官方 App Server 執行真實工具與 CPU Jobs。付費登記、腳本驗收和另行核准的 OAuth 探索協議各有自己的分母。

## 預檢、登記、執行

在裝妥既有離線發行資產的候選 checkout 中執行。OUT 指向倉庫外尚未存在的絕對路徑，CLASH_BIN 指向本機 Clash。執行下列唯讀預檢時不需要提供憑證內容。官方宿主在既有登入環境讀取去識別化帳戶和模型目錄，依本機規則走 Clash；預檢不啟動 thread、turn 或供應商推理。未具完整入場條件時回傳 2，沒有建立研究格。

```bash
.venv/bin/python -B acceptance/v623/live/pilot.py preflight --host live
bash acceptance/v623/live/rehearse.sh "$OUT" "$CLASH_BIN"
.venv/bin/python -B acceptance/v623/live/adjudication.py --help
```

演練由同一個正式入口分別執行登記、依序開始四個原始格，再查核實際收據。各研究者取得相同問題、兩臂共有的科學方法、條件式材料、時間、額度及證據政策。可委派臂另有 data_inventory@3。腳本在第二格收到介入後才開盤點，第三格可以按原值及缺口提早停止，第四格可選擇自行完成。生產 Lead 依實際計畫與所見證據選擇做法。比較可取用的權限，同時保留實際讀取、送達、引用和生效行動。

以下指令展示硬額度系列的正式操作。LIVE 指向新工作目錄，ROSTER 為事前指定的獨立讀者及裁決者清冊，GRANT 為可由部署信任來源查證的本系列批准。登記要固定候選 SHA、方法、協議、計畫卡、原料、裁決規則與精確清冊摘要。現有通用登入、範例申請文件、其他系列的批准無法啟動此系列。

```bash
pilot=(.venv/bin/python -B acceptance/v623/live/pilot.py)
"${pilot[@]}" preflight --host live --roster "$ROSTER" --approval "$GRANT"
"${pilot[@]}" register --host live --work "$LIVE" --roster "$ROSTER" --approval "$GRANT"
"${pilot[@]}" run --host live --work "$LIVE" --slot 01 --approval "$GRANT"
"${pilot[@]}" status --work "$LIVE"
"${pilot[@]}" stop --work "$LIVE" --slot 01
"${pilot[@]}" reconcile --work "$LIVE" --slot 01
"${pilot[@]}" resume --host live --work "$LIVE" --slot 01 --approval "$GRANT"
```

**run 與 resume 首次可能產生正式費用。** 每格完成後依 rubric.json 完成獨立機會鎖、結果包與外部簽名評審，再以相同命令開始下一原始格。已有終態的原格留在原處；同一個對話可從原工作目錄繼續。截止時間由首次啟動算起。有未決物理請求時只向同一部署按原 ID 做免費對帳。當證據不足、需要裁決或費用仍無法查證時，後續格會停在其原分派。

## 宿主與帳本

兩種部署均透過同一個僅環回監聽的 Responses 路徑，在同一筆物理請求上執行 quote、reserve、send 與 settle。GX 查核 App Server 原生 thread/turn、Run、角色、lease、作用域、model 與 effort，並對齊原始 SSE 完成事件及原生逐 turn 計量。原始請求與原始回應都有摘要，帳務有資料庫可重放。provider 全部實體請求的 input／cached input／output／reasoning 依回執揭露，未得確認的美元費用保留為未知。每個原生會話在該格私有 CODEX_HOME 下執行；官方模型供應實作必須由部署端提供原生模型目錄及不可超額契約。宿主回顯的模型名稱只證明其會話配置。正式推理另需來自已核准上游部署的帳戶、模型與原始結算證據。

stock Codex OAuth 沒有硬限制所需的逐請求最高可計費量及原 ID 收據對帳契約。其唯讀登入與目錄可驗，hard-cap 系列在這種部署仍為 HARD_CAP_UNAVAILABLE。安裝受信的計量部署時，用 deployment.py 文件與實作描述的 OS 所有綁定、不可由申請者替換的實作摘要、遠端身分及可驗請求上界。可查證批准固定同一個部署、帳戶、系列、候選及清冊。不得把本機腳本或名字叫 openai 的自訂端點列成新真模型。

## 查看回執

- OUT/network.json 證明測試命名空間只保留環回通路及拒絕外網的 Clash。OUT/verification.json 保留四個原始 Run 的效果、實際 turn、實體請求和計量合計。
- OUT/run/registration.json、source-pins.json、custody/ 保存四格分派、當時程式和材料；NN/assignment.json 是首次原計畫後的 GX 派送事件。原始 Job 及產物在 NN/state。
- NN/codex/sessions、native-host.json 和 raw/physical-http.jsonl 可交叉核對原生角色、操作與供應商收據。NN/accounting.json 與整批 funding.sqlite 區分實際確認、未知曝險和原始費用。只向經授權的操作員提供原始會話。
- adjudication/ 的匿名機會包、凍結窗口及結果包供獨立重評。離線腳本的執行效果可以查證；獨立人員未簽署的科學評分顯示為待審。
