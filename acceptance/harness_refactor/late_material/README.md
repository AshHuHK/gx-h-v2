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
