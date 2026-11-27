# 官方 Codex 实跑：同一方法和 workflow 在新底物上复用

两道独立问题都已自然结题。海藻糖目录里，3 个酿酒酵母候选有 2 个带实验文献 PMID；蔗糖目录里，1 个候选有 0 个带该指针。后继 Lead 确实检索并展开了父题保存的 workflow，在另一份数据上调用同一版本的分析工具。两次结题均引用实际收到的具名数值行。[SQLite、宿主与产物联合回执](evidence/native-recipe-lineage.json)

| | 海藻糖父题 | 蔗糖后继题 |
| --- | --- | --- |
| Run | `gxr_01m8d9xyaych3h1z2b` | `gxr_01m8daq09da52k64ar` |
| 官方宿主回合 | 6 | 3 |
| 开发／校准 CPU Job | `gxj_01m8da00qebs016yyw` | `gxj_01m8db05gcf0h9t489` |
| 覆盖计算 CPU Job | `gxj_01m8da74da9jr15fc4` | `gxj_01m8db0633cg9nc1m4` |
| Lead 实收读数 | `gxa_01m8da74tz13kpm8tt`，2/3 | `gxa_01m8db06ms71pg408p`，0/1 |
| 生效结题事件 | 147 | 255 |

父题 Lead 安装了 `record-coverage-8902880ed9d712bf6baa5899@1`，计算后实际调用 `gx_save_recipe` 保存 `accession-pmid-coverage@1`。保存者绑定的两个源 Job 完成摘要都与原始执行清单一致。后继题在恢复后的工作区中取得相同 recipe，SHA-256 为 `74d6d077301821523c0178e9ef5d2ae4ff03871f19ced4eec3c5bd3ee0cf7a58`。后继 Lead 选择现有方法，两次调用 `gx_instantiate_recipe`。第二次传入正确的本地分支名后，两个节点的输入、参数、问题、相互依赖和分支都与实际采纳图吻合。两份署名输入的内容摘要都发生变化，两次覆盖计算的包摘要和方法清单摘要分别相同。

**有一项实际重复计算。** workflow 保存了开发校准和测量两个步骤。后继题重新执行了校准 Job，校准的原文摘要与父题相同；软件包复用并未省去这个 Job。过程中父题有 8 次决策提交遭拒，后继有 11 次。四个 CPU Job 均成功，父题另有一个已取消的图节点未派工。两个 Run 一共 9 个 GX 接收的宿主回合，可逐个匹配到部署保存的官方 Codex session 与 turn，记录的模型均为 `gpt-6.2-astra`，推理档均为 `max`。

父题 Lead 决定优先查阅 P32356 和 P48016 指向的论文，对 P35172 单独补检。后继 Lead 发现唯一的 P00724 在两表均有匹配，但该行的 PMID 为空，因而保留分母并报告缺口。这些结果衡量本次数据库快照的指针覆盖。研究者仍需阅读原论文，判断具体酶学观察能支持什么。

从产品根目录执行以下只读复核。命令使用两个 Run 的原始状态、相应的 Codex 会话及产物文件。

```bash
gx_checks=../gx-science/capabilities-check
.venv/bin/python -B acceptance/science_capabilities/native_recipe_lineage.py \
  "$gx_checks/live-record-coverage" "$gx_checks/live-record-successor" \
  gxr_01m8daq09da52k64ar > /tmp/gx-native-recipe-lineage.json
cmp /tmp/gx-native-recipe-lineage.json acceptance/science_capabilities/evidence/native-recipe-lineage.json
```
