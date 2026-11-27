# 开发并复用一个可校准的证据覆盖方法

研究问题是酿酒酵母水解酶数据库条目中，有多少条目带有可追到实验文献的 PMID。海藻糖和蔗糖是两个独立问题，分别使用各自的 Swiss-Prot/Rhea/ENZYME 快照。原始查询、数据库版本、源文件校验和、逐条定位保留在各自的 `candidates.json`。每个 `pointers.json` 将 `candidates[].experimental_paper_pointers[]` 展成行；无指针条目保留一行 null。这两个输入同源，只能用于评价该目录的证据覆盖。文献是否真的支持酶学结论仍需读原文。

`method-source.md` 是随题的读法和局限卡。所据原论文为 Ahmad 等（2025），DOI 10.1093/nar/gkaf394，“ID Mapping service”首段与表 3。GX 历史库的 Paper2Skill 文本 `db-uniprot-api-paper/references/paper.md` 的 SHA-256 为 `06081d2e31736e4d9cdd7e6de4d1aa5e00a99121b6534d3ef48c15cd38eb5ce8`。本次复用了 gx2understand 的定位与边界、Paper2Skill 的原文导航、GX-Paper2Agent 的独立校准和失败样例要求；本方法的代码由 GX 独立实现。作者原文讨论 ID Mapping 服务，对字段级覆盖估计量的定义和验证由本实现负责。随包提供所用短引文，运行无需挂载旧仓库。

Lead 查看数据、选择标识与读数字段和一段唯一原文。独立 CPU Job `derive-record-coverage@1` 核对引文、编制有版本的方法并运行手算对照。开发产物含原件哈希、引文位置、字段映射、固定 Python 源码哈希和校准回执。`gx_install_record_method` 只接收该 Goal 已完成开发 Job 的具名输出，安装后由现有能力版本机制绑定到该题。下个决定可以派真正的具名输出 `coverage.json` Job。同一标识的多个 PMID 只增加一次分子，空值与完全匹配不到的情况分别报告；数值零是合法实测值，空分母明确失败。

从产品根目录启动第一题。只使用部署中的官方 OAuth 入口。

```bash
gx_work=$(mktemp -d)
gx_state="$gx_work/state"
gx_oauth() { .venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$gx_work" "$@"; }
gx_oauth research --state "$gx_state" --run-key trehalose-record-method \
  --goal-file examples/record-method-authoring/trehalose/question.md \
  --goal-config examples/record-method-authoring/trehalose/goal.json --research-effort deep
```

第一题正式关闭后使用其实际 Run ID。后继配置仅列出父题实际完成计算且仍启用的方法作为可选项。Lead 可从目录发现保存的 recipe，按原 pin 启用方法，将新题原件作为输入。重复发送相同消息 ID 会复用相同后继 Run。

```bash
.venv/bin/python -B acceptance/science_capabilities/record_method.py followup \
  --state "$gx_state" --run-id "$gx_parent" > "$gx_work/successor.json"
gx_oauth message --state "$gx_state" --run-id "$gx_parent" --message-id sucrose-coverage-v1 \
  --text "$(cat examples/record-method-authoring/sucrose/question.md)" \
  --successor --successor-config "$gx_work/successor.json"
bin/gx-p0 conversation --state "$gx_state" --run-id "$gx_parent"
gx_oauth research --state "$gx_state" --run-id "$gx_child"
.venv/bin/python -B acceptance/science_capabilities/record_method.py report \
  --state "$gx_state" --run-id "$gx_child"
```

离线工程验收运行正常 Goal、受控的原生回调和真实 CPU 子进程。JSON 回执记录两个 Run、不同原件、读数、校准、同一 recipe 摘要和结题引用。正式模型运行应另行保存当次的真实宿主回执。

```bash
GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  .venv/bin/python -B -m unittest acceptance.science_capabilities.test_derived_method
```
