# 科學盲評原件與解盲對照

五份評閱包均保存評閱者收到的原始位元組。每包包含問題、科學原件、評分條件和匿名答案。下表列出解盲後的版本與分數，五個 SHA-256 均已和當次盲評回執的 `packet_sha256` 核對。

| 場次與評閱包 | 解盲後的研究臂 | 科學判據 | 原包 SHA-256 | 逐項判讀 |
|---|---|---:|---|---|
| 長題首試 [report-1](long/field/anonymous/report-1/packet.json) | GX 候選 `362157ae` | 4/7 | `50304bc3323571e6a2695a030ca8994ca1d8672c624f77dbd2d3e937b6bb9589` | [首試回執](long/field/results.json) |
| 長題首試 report-2 | GX 起點 `4b6aa93` | 無終稿可評 | — | [首試截止紀錄](long/field/results.json) |
| 長題首試 [report-3](long/field/anonymous/report-3/packet.json) | 原生 Codex | 7/7 | `98b9017694359d326c72aacc0889078a14474d8c298d8fcdf7873aa15a7d10b3` | [首試回執](long/field/results.json) |
| 長題回驗 [7241216d6ec223f407f9](long/followup/anonymous/7241216d6ec223f407f9/packet.json) | GX 修後 `6077e26` | 7/7 | `45618b31bd81e19171923dd90a2c785a8b3909bf093aedba0dccc85db6fc89a4` | [回驗回執](long/followup/results.json) |
| 短題[首試評閱包](short/field/independent-review/packet.json) | GX 候選 `362157ae` | 7/7 | `0e5f64d131d54a59a9e87690baca1cb247ac347392ca1f33deb936232bf76c10` | [首試回執](short/field/results.json) |
| 短題[回驗評閱包](short/followup/independent-review/packet.json) | GX 修後 `6077e26` | 7/7 | `15831b4bae7f53e8e44a1c5a43788eea819f1208556a41e4082c7eb29ef431af` | [回驗回執](short/followup/results.json) |

長題起點臂在 900.040 秒截止時仍為 OPEN，沒有終稿可交給評閱者，因此三臂共產生兩份首試盲評包。三個匿名標籤與研究臂的原始對照檔摘要為 `b5d458effc9be17efb9fa4c0de90950573bcc4a25cda1e448a19c2ecde7f2bb6`。[首試原始解盲表](long/field/anonymous-map.json)
