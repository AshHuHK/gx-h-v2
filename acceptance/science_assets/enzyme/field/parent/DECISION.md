# 完整结题决定

品管頁可刊登 S. cerevisiae S288C 的天然 α-葡萄糖苷酶 MAL12（Swiss-Prot P53341）與 MAL32（P38158），序列版本均為 1，兩者均註記 EC 3.2.1.20。指定主反應 RHEA:68796 為 D-maltose + H2O = alpha-D-glucose + D-glucose，列 EC 3.2.1.48。兩個蛋白條目均缺少此反應連結、個別方向及可歸屬的麥芽糖測定條件。MAL12 的 BRENDA 3.2.1.20/km_value/131 使用 4-nitrophenyl alpha-d-glucopyranoside，3.2.1.20/specific_activity/33 未指明底物。兩條各自記載 pH 6.6、37 °C，各計一次。已保存程序 s288c-disaccharide-archive-qc@1。交接原件為 query-a.json（gxa_01m8fdkea3wrcvsntp）及 parent-records.json（gxa_01m8fdmcaez3kstbtj，SHA-256 6ac837e862e61b7ea98f7a7374340813bd1f8b23f713eccd74eb9aca39a6e907）。在下一個查詢中可將 substrate 換成 isomaltose，保持 taxid 559292，以已存程序查詢。若需跨二糖比對，以新查詢作 query，將本次 parent-records.json 原檔作 prior_records 交給 yeast-archive-compare@1，核對來源與序列身分。

已逐筆核對查詢原件及 parent-records.json。MAL12 與 MAL32 各有 ENZYME 分類來源行，兩筆的 reaction_links 都是空陣列。MAL12 所連的 BRENDA 條目分別記載其他具名底物與未指明底物，MAL32 沒有連結測定，SABIO-RK 也沒有連結條目。這些記錄足以決定本版品管頁的刊登範圍。重跑相同快照只會再次讀取同一批原始記錄。可重用程序已儲存並在目錄核對。

## 适用范围与未决问题

- 在這些封存版本內，仍缺可把指定反應及催化方向連到任一蛋白的條目，也缺可歸屬到該蛋白及麥芽糖的測定條件。兩種蛋白對麥芽糖的實際活性仍待同底物原始測定確認。

## 依据

- gxa_01m8fdkea3wrcvsntp
- gxa_01m8fdmcaez3kstbtj

决定 gxa_c2d6062f5205f2281611f8863c21a83278776fd4474d4937734c33b1c5098ee9 · 结题事件 80

[原始研究记录](result.json)
