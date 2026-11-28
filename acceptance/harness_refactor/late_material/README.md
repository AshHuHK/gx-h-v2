# 定向晚件在冷恢復後送達

在正式 Gate 2 Run `gxr_01m8fs36kqvx147ysh`，事件 87 把第二站原件授給仍開放的隔離資料員，88 登記 PI 回饋。官方宿主恢復時確認收到的文字 SHA 與事件 21 的舊簡報一致。這個回合只呼叫了提交報告，事件 116 的原話仍說第二站沒有提供。這筆報告與事件 79 的另一份報告保留原判。[宿主與 GX 交付核對](original-delivery.json)

在 `gx.harness/2` 普通研究中，PI 給某個研究員確實新增原件時，GX 會保留授權事件所針對的舊決策任務 ID，原子釋放該短任務的舊租約。相同模組的下一份原生簡報納入新原件和已路由的 PI 原話。模型自行決定採信、拒絕或保留疑點。已列在簡報可讀範圍的原件可重送，既有任務繼續有效。新短任務沿用原模組經費和准入規則。

三個普通研究入口控制涵蓋以下時序。

- Main 路由私有晚件後收到 SIGKILL。原 Run 重開後舊回呼無法提交，新的原生回執與簡報 SHA 相符。資料員讀到定向原件，提交對應回饋的報告。重送同一回執或已知原件後任務數維持不變，兄弟模組無權讀取，原任務失敗計數不變。[同 Run 冷恢復回執](cold-receipt.json)
- 收件者已有 CPU Job 在跑時，定向晚件喚醒後繼短任務。原 Job 以同一 ID 完成，另一位研究員先前的報告與 SHA 保留。[CPU 與報告回執](ongoing-job.json)
- PI 原件入帳後委派被撤回。重開同 Run 後模組維持撤銷，舊租約讀取被拒。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a python -B -m unittest -v \
  acceptance.harness_refactor.late_material.test_directed_delivery
```

[基線重現](before.log)和[修後三項驗收](three-controls.log)使用可控原生模型回呼與真實本機 Job。正式模型在修後版本的判讀，須由後續 Run 的實際回執另行確認。


## 一個已領研究任務收到晚件

正式復驗 `gxr_01m8fsxs8ntscbmg1m` 在 `b5545e5` 新增授權 72、取消 73 及回饋 74 均成功。資料員的唯一 worker 名額在 33／34 已扣，82 由 GX 產生 PARTIAL；該資料員沒有處理晚件。該模組帳面已結算 125071 token（含 65536 估算），額度仍有 6001 token、4 次 API。84 已將模組剩餘預留退回。這個已封存的結果保持原判。[事件與預留核對](one-slot-live-baseline.json)

以 PI 原件的授予和中止同一任務的轉移事件識別此次交接，將新 brief 發給原委派；後繼任務回執 `v62.module.worker_task_continued` 指向原任務和授權事件。每次實際原生 turn 仍獨立預留 API、token 並結算。真正完成一項決策後再要求新任務仍需要 worker 名額。

普通研究離線控制在同樣 1 個 worker、6 次 API、131072 token 額度及 125071 已結算用量下重現過「No active decision for module」。修後發出含第二份原件的原生 brief，該回合僅預留剩下的 6001 token 和一次 API。兩次輪詢後仍只有一次 worker 扣款，一份後繼回執，沒有新 Job；舊短任務與其計量等待仍可追溯。後繼完成科學決策，再申請真正的新任務時 GX 如實交回名額耗盡。上述四個控制通過。兩次原生呼叫的用量由測試宿主回執提供。

## 已讀私件和公開報告

在 `48dfdb0` 的正式 Run `gxr_01m8fthqsy7h6t5z5x`，同一名研究員讀取晚件的回執 82、96、124、133 均已入帳。提交 `call_6daf0eb139f64d788d1a3a386033d37c` 及 `call_8447f064b48247b1afa26672a8cb7b98` 把晚件原件列在公開引用內。`call_807201cab22e483aa0b47fdd7d394bc1` 移除頂層引用後，個別 finding 仍引用同一私件。GX 先前一律提示「須讀取」，導致反覆讀取。原報告回執均保留。

修後，受領人引用自己已實讀的私件或其登記摘錄，會收到 `SCOPE_DENIED` 及原因 `branch_private_report_source`。公開報告會把引用帶給其他研究員。需要公開核驗時，可透過決策的 `pi_questions` 請 PI 另供可共享的原件；若仍缺合法來源，可提交記錄出版缺口的部分報告。獨立研究員猜測私件 ID 時仍只看到普通的未具備來源回執訊息。研究員可引用其已讀的公開來源，支持該來源實際涵蓋的判斷。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a python -B -m unittest -v \
  acceptance.harness_refactor.late_material.test_report_visibility
```
