# S288C 表格歸屬追試

本題保留首輪 `gxr_01m8fq45p3rfgk2cv5` 的原始表現。它另用一個普通 medium Goal，交給模型的資料只有 `question.md` 與原出版 JATS。論文為 *Similarities and differences in the biochemical and enzymological properties of the four isomaltases from Saccharomyces cerevisiae*，PMCID PMC3953731.1，DOI 10.1016/j.fob.2014.02.004。

`preregistered.json` 記錄題面與原件的 SHA-256、指名表格的位置、評分順序及科學判準。原文表格內有該列的底物名稱、計量單位與各自然酵素數值；摘要沒有該底物名稱。PI 沒有指定檢索工具或底物。Lead 的委派內容由現場 Run 另行記錄，它會影響能否歸因為子研究員自主發現。

從完整產品目錄啟動、接回同一 Run，並匯出實際原生回執：

```sh
.venv/bin/python -B acceptance/harness_refactor/field/yeast-followup/run.py ../harness-refactor-live
.venv/bin/python -B acceptance/harness_refactor/field/evidence.py ../harness-refactor-live yeast-followup
```

若直接持有相同公開 JATS，啟動命令可傳入 `--article /path/to/article.xml`。啟動器獨立比對預登記摘要，只用本站現有官方 Codex 訂閱與 Clash。計分時區分選法、得知底物、嘗試與正式採用查詢參數、Job 完成、作者具名的報告、GX 交回的部分結果，以及 Lead 的後續決定。
