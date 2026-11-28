# 同題正式工程復驗：正確取消舊簡報，但收件席位被耗盡

Run `gxr_01m8fsxs8ntscbmg1m` 使用修補 `b5545e559f2fb2586068716789c9590a0498b4f4`。部署逐檔核對 290 個 Python 檔。兩份材料、題面、唯一離線訊息、選擇 OPEN 收件人的規則及兩個方法 pin 與首次試驗相同。兩次運行的 Lead 自主選擇了不同的研究員分工和子預算。

| 事件 | 實際效果 |
|---|---|
| 64 | 原件審核員先完成首站報告。仍 OPEN 的定量核驗員成為這次的原收件人，其 scope 是 `module:module_9be3a61d84ebd7893bb95196aacc5c71`。 |
| 67–70 | Driver 正式退出 -9。同一 Run 正常接收一次第二站附件，69 登記、70 入列。 |
| 72–75 | 新 Driver 在 72 記錄正確 grant 和原短任務 `gxt_01m8fsz1hkpn4c1myz`；73 取消過時任務，74 保存 PI 原話，75 完成路由。 |
| 82 | GX 自行產生 PARTIAL 報告，唯一缺口為 `module worker task allowance exhausted`。Lead 已給該席 1 個 worker task、6 個 API call、131,072 token；同席位因系統自行更新快照而無法取得第二份簡報。這份報告作者為 GX。回執中沒有一個後繼官方收件回合、原件實讀或科學採信。 |
| 93–94 | Lead 依已知原件只報 Little Falls 首末下降 13.53%。93 採納暫定判斷，94 正式請 PI 提供第二站。Run 保持開啟、等待 PI；未重投資料或提供答案。 |

已支出的用量和真實席位限制均留存。普通入口沒有提高子預算。現場沒有執行 CPU Job，前一席位報告仍是事件 64。

```sh
.venv/bin/python -B acceptance/harness_refactor/field/late-water/check_repair.py acceptance/harness_refactor/field/late-water/repair/receipt.json
```

[修補部署與預登記](preregistered.json) · [首輪修補正式回執](receipt.json)
