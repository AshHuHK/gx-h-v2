# V5 引用的 A01–A22 與本輪範圍

原矩陣為各案例指定干預、後端及證據。下表的狀態指該原案例是否按原規格整項複跑。右欄只索引 V6.1 涵蓋的相關行為。REG01 使用獨立的 P2A／v3.1 回歸分母，V6.1 的結構放行使用本次的新舊工程門檻。[原矩陣及具體判據](../../p2/ACCEPTANCE_MATRIX.md)

| 案號 | 原驗收範圍 | 原規格本輪執行 | 相關實驗 |
|---|---|---|---|
| A01 | 全部 Codex 退出。C14 决策服务长时间不可用 | NOT RUN | 離線恢復與工作接管見 V61-UPGRADE、V61-MODES。 |
| A02 | C03 Agent Task Session 修改图，另测改 Goal、预算 | NOT RUN | 本輪測試原生 Goal 授權與過期 head。 |
| A03 | C04 人类接管/撤销 Decision Lease 后迟到提交 | NOT RUN | 原 V6-DECISION／V6-NATIVE 含租約失效負例。 |
| A04 | C01 将较弱 Goal 阈值伪装成方法参数。C02 Codex 提高硬预算 | NOT RUN | 首版委任上限與既有父／分支額度有獨立負例。 |
| A05 | Recipe 默认值展开、精确摘要固定 | NOT RUN | Run composition 保存 package 及方法 pin。 |
| A06 | 插件自带新的 Capability | NOT RUN | V61-CSV 實際安裝新能力。 |
| A07 | C08 Node 直接提交未登记 Job。C12 隐藏无界生成/筛选/重试 | NOT RUN | 原案例所指定的故障或領域後端未在此版本獨立執行。 |
| A08 | C06 Evaluator 只返回 `accepted=true` | NOT RUN | 原案例所指定的故障或領域後端未在此版本獨立執行。 |
| A09 | C07 未经批准替换 Goal Evaluator | NOT RUN | 原案例所指定的故障或領域後端未在此版本獨立執行。 |
| A10 | Attempt 先于外部 submit | NOT RUN | 待派與已受理兩種邊界由 V61-UPGRADE 注入。 |
| A11 | C09 调度器已接受，GX 尚未保存返回句柄就崩溃 | NOT RUN | 本版測了本機 Job 已受理時的故障；原案例另指定外部 scheduler。 |
| A12 | 求解器 checkpoint 续算。C10 损坏/不兼容的 checkpoint | NOT RUN | 原案例所指定的故障或領域後端未在此版本獨立執行。 |
| A13 | C05 两个不同 amendment 使用同一 base | NOT RUN | 原 V6-DECISION 含相同 base 競寫。 |
| A14 | C13 分支等待决策时同 Run 的独立分支继续 | NOT RUN | 有並行分支與凍結上下文測試；實體並行時序另按 Run 核對。 |
| A15 | 只更改下游节点，无关输出复用 | NOT RUN | V6-DISPATCH 及蛋白重評檢查工作重用。 |
| A16 | C11 新 seed 夹带旧 exec_key | NOT RUN | 原案例所指定的故障或領域後端未在此版本獨立執行。 |
| A17 | v3.1 terminal、结构修复、冷恢复。C18 不支持的 Codex 版本 | NOT RUN | REG01 重新執行 v3.1 相容項目。 |
| A18 | Native Child 范围、恢复与用量。C17 越界读或租约过期 | NOT RUN | REG01 另有官方 Child 及用量協議回環。 |
| A19 | Brief 的摘要、大小、新鲜度和按需读取。C16 过期 Brief | NOT RUN | 本次 V6-NATIVE、CSV 均核對實際下一個 thread 的可見簡報。 |
| A20 | 独立 Plugin Graph 不依赖 science_grid | NOT RUN | V61-CSV 使用外部方法與通用 Goal 圖；原題指定獨立領域的 Graph Recipe。 |
| A21 | 后端拒绝未批准的容器摘要 | NOT RUN | 原案例所指定的故障或領域後端未在此版本獨立執行。 |
| A22 | C15 代理指标通过、缺少正交证据 | NOT RUN | 蛋白案例從原座標獨立複算，CSV 從原始數列獨立計算。 |
