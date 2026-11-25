# 公開四格的實體請求與經費

在 release 根目錄檢查正式入口和執行零付費控制組。

```bash
.venv/bin/python -B acceptance/v623/live/pilot.py preflight --host live
.venv/bin/python -B -m unittest -v acceptance.v623.test_live_funding
```

預檢只會查官方宿主、路由、模型目錄、部署計量契約與授權。當前鎖定的 Codex 0.169.0 原生 OAuth 沒有涵蓋所有計費 token 的請求上界。缺少計量部署時會列出 `HARD_CAP_UNAVAILABLE`。沒有本批授權時另列 `AUTHORIZATION_MISSING`。日後接通經驗證的部署時，`run` 與 `resume` 首次可能付費。

## 現有正式部署如何接入

系統管理員可在 `/etc/gx-h/live/` 安裝三件既有部署材料。目錄、檔案與上層目錄由 root 擁有，只允許管理員寫入。應保留供應商身分、契約文件、實際強制上限和回執查詢的驗證紀錄。批次操作者不能用命令列參數改變這個目錄。

| 檔案 | 內容 |
| --- | --- |
| `provider-contract` | 所選正式計量部署的原始契約。須涵蓋每個請求全部可計費輸入、輸出、快取與推理，並能免費查詢已送出的同筆請求。 |
| `owner.py` | 管理員安裝、已驗證的供應商介接程式。固定 factory 為 `open_owner(*, proxies)`。程式負責在同一正式部署上驗證供應商、取得上界、送出和查回原始使用量。所有外部連線遵守傳入的 Clash 路由。 |
| `binding.json` | `schema=gx.priced-responses/1`，加上 `adapter_sha256`、`contract_sha256`、`provider_origin`、`deployment_id`、`contract_id`、`account_ref`。最後一項使用可公開的帳務代號。 |

供應商介接程式提供以下介面。GX 信任部署管理員安裝的程式驗證供應商回執。對輸入長度的估計、回應中的累計用量或本機產生的聲明，均不足以核准這項部署。

| 呼叫 | 所需行為 |
| --- | --- |
| `deployment_receipt()` | 免費驗證當前正式供應商、帳戶與契約後，返回 `verified`、`provider_origin`、`contract_sha256`、`deployment_id`、`contract_id`、`account_ref`、`provider_evidence_ref`、`model`、`efforts`、`all_billable_tokens_bounded`、`free_lookup_by_original_request_id`。回執只能含可公開的代號。 |
| `model_catalog()` | 供正式原生宿主使用的 Codex 格式模型目錄。 |
| `quote(request_id, body)` | 免費取得並驗證供應商強制執行的 `Quote(request_id, payload_sha256, maximum_tokens, contract_id, quote_id)`。上界涵蓋單次實體請求的全部帳務。 |
| `send(request_id, body, quote)` | 供應商以同一已認證的聯線身分執行獲准請求，回傳 `MeteredReply(request_id, quote_id, wire)`。每次呼叫最多產生一次計費效果。所有可能計費的重試、收尾或工具後續請求個別入帳。 |
| `lookup(request_id)` | 免費查詢原始供應商請求。取得原回執時回傳相同格式的 `MeteredReply`，不確定時返回 `None`。 |
| 物件屬性 | `source="PRODUCTION"`、`deployment_id`、`contract_id`。測試替身標記為 `SCRIPTED_LOOPBACK`。 |

`wire` 是可直接交還官方宿主的 Responses SSE。終態事件應有唯一供應商回應 ID，並附最終輸入、輸出及總用量。輸入快取與推理 token 是各自母項的子集。有用量的失敗及不完整回應仍計入已花費用。缺少帳單時原預留持續占用額度。美元金額以未知保留，對接的 token 回執不提供美元結算。

## 外部批准

可沿用部署管理員的批准來源。管理員把經確認的精確系列授權放在 `/etc/gx-h/approved-series/GX-V62C-PUBLIC-COVERAGE-LIVE-01.json`。另一項選擇是由已登記在 `/etc/gx-h/live-pi.allowed_signers` 的 `gx-live-pi` 主體以 OpenSSH `gx-series-spending` namespace 簽署授權的完整原始位元組。將 `grant.json.sig` 與 `--approval /path/to/grant.json` 放在同一處。這兩種來源皆由外部權責方決定。

授權須包含 `schema=gx.series-spending/1`、`execution_authorized=true`、`series`、`candidate`、`protocol_sha256`、`model`、`effort`、`cost_mode=HARD_TOKEN_CAP`、`tokens_per_slot=1000000`、`tokens_whole_batch=4000000`、`all_physical_requests=true`、`paid_judges=false`、`deployment_id`、`contract_id`、`account_ref`、`approval_ref` 與有時區的 `expires_utc`。執行預檢讀取含指定審閱者名單的 `protocol_sha256`。本系列採人工獨立裁決；另一科研系列或付費裁決需要另外的核准。

## 冷恢復和覆額

原子預留同時計入單格與全批。若在預留後、提交送出意圖前退出，冷恢復可接續同一筆。從送出意圖提交起，斷線後先以原請求 ID 免費查回。查不到時保留原預留；同一 scope 的新 ID 暫停准入。結算後重送會取回已保存的供應商回應。若供應商超過先前承諾的上界，GX 會把完整實際用量寫入帳本並暫停後續送出。
