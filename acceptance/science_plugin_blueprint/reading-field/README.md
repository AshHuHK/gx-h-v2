# 普通 Goal 的閱讀技能首選

這個單案觀察的是 Lead 面對陌生問題時如何選擇閱讀技能。問題只問天文台可否從附帶論文的夜光變化，比較各國經濟成長。材料來自 A2 已取得的 Kyba 等人 2017 年全文，這次單篇入場，題目與 A2 的路燈及居民觀星判斷不同。研究未重新下載論文。新 Run 使用獨立狀態、預設可用工具與研究模組、deep effort、正式 Codex OAuth 和本機 Clash。原初觀察條件與原件摘要均在研究啟動前寫入。[原問題](question.md) · [預註冊](preregistration.json) · [研究原件 DOI](https://doi.org/10.1126/sciadv.1701528)

## 復核與恢復

本次入場設定的 `publication.xml` 是 A2 準備程式取得的 `materials/trend.xml` 同位元副本。重新組裝材料時，將這份檔與本目錄的 `goal.json` 放在一起，檔案摘要由 Goal 核對。正式研究使用既有訂閱。[原件取得程式](../a2/prepare_materials.py) · [入場設定](goal.json)

原始 Run 為 `gxr_01m8f7ypt5cm2e6rkq`。狀態目錄為產品根目錄相對位置 `../gx-science/plugin-blueprint-live/reading-field/state`。使用既有 OAuth 與原部署，以原 Run ID 繼續或從同一 SQLite 庫唯讀整理原始回執。

```bash
study=../gx-science/plugin-blueprint-live/reading-field
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study" research \
  --state "$study/state" --run-id gxr_01m8f7ypt5cm2e6rkq
.venv/bin/python -B acceptance/science_plugin_blueprint/reading-field/collect.py "$study/state/gx.db"
```

讀取段落的分母是成功取得 `receipt_id` 的具名段落呼叫；分頁列出段名、目錄的短卡、打開完整技能卡、原文摘錄和生效決策各自認列。同一位作者曾有數次取段，一段也可能被兩個研究者分別讀取。

## 官方模型首個選擇

首個查詢自行從科學問題組成字串，回傳名單第一項為 `gx2understand@1`。Lead 隨後打開另一張可用技能 `source-evidence-review@1` 的完整卡及離線閱讀模組的說明，並按具名段落讀取真實 JATS 原件。另有受控精查證明兩張當代閱讀卡均可取用，過往版本為僅供參考，缺少原文工具的模組無法啟用當代深讀技能。[獨立受控對照](controlled.json)


原件中第 p21 段報告，國家夜光與 GDP 的水準相關按人均及全國總量分別為 0.76 和 0.85。2012 至 2016 年間，輻射亮度變化與 GDP 變化的跨國秩相關僅 0.17，亮燈面積變化與 GDP 變化為 0.24。第 p48 段交代 134 個有完整 GDP 和人口資料且兩個年份均達亮燈面積門檻的國家。Lead 在決策 86 引用這個差別並等待係數與分母覆核。獨立檢查直接在原始 XML 核對數字，並重算 GX 段落摘要。[段落與數字覆核](numeric-check.json)

## 結案

事件 101 收錄 Lead 判斷，107 採用結題，108 正式關閉。第二個 Lead 回合覆核子研究員交付的兩筆原文摘錄，結論為可比較觀測到的夜光變化；本文的秩相關不足以支撐可靠的逐國 GDP 成長排名。三個官方原生回合包含兩個 Lead 回合及一個模組回合，具名段落閱讀回執分別累計頂層 61 筆、模組 11 筆，外部 Job 為 0。模組以 GX 自動署名的 PARTIAL 結束。兩筆原件覆核與最終採納都另有回執。

本題 `gx2understand@1` 完整卡開啟數為 0。它在第一個結果頁的 35 筆候選排第 1，Lead 實際選讀 `source-evidence-review@1` 和模組說明。這項觀察支持候選已可發現，以及模型確能自主改選另一張已安裝技能。單一問題無法估計兩張卡的使用率，也無法從取卡時序單獨推知對結論的因果效果。[正式結題](DECISION.md) · [研究報告](REPORT.md) · [逐次查詢、正文閱讀與決策](receipts.json) · [GX 科研記錄](result.json)
