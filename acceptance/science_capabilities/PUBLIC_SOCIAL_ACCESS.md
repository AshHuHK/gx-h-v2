# 微信公众号与小红书的公开文章入口

2026-11-27 01:36 UTC 对历史资料中的两个真实公开链接进行了匿名获取预检。两个出版方均返回 HTTP 200 的 robots.txt；适用于 GX 的规则禁止自动读取相应文章路径。本次收到的正文数为 0。两条记录都不能支持当前酵母科学问题的结论。[实际回执](evidence/public-social-access.json)

| 平台 | 档案中的公开文章标识 | 出版方规则与本次取得物 |
| --- | --- | --- |
| 微信公众号 | `8HYZMtpJi892jYm0U_5TSA` | 通配客户端禁止 `/s/…`。仅获取了规则文件，回执绑定实际归档元数据摘要。 |
| 小红书 | `6ada02c3000000002a0386c8` | 适用于本客户端的规则禁止 `/explore/…`。已命名搜索引擎的条款只适用于对应客户端。 |

来源登记保留两种现有输入。只有公开链接时，`social-source-intake@1` 产生待跟进线索；研究者持有并授权使用本地导出文本时，可登记原件摘要、段落定位与待核的原始论文。该渠道中的观点仍须用论文、实验读数或数据库原件另行核对。[本地导入回执](evidence/social-intake.json)

同一检查可用原归档复跑。脚本只读取两个出版方的规则文件，经指定 Clash HTTP 代理；不提交凭据、文章内容或笔记检索请求。规则随时间可能变化。返回 `public_article_access_unverified` 意味着仍需验证公开页面能否实际读取。

```bash
.venv/bin/python -B acceptance/science_capabilities/public_social_access.py \
  ../../../GX-harness/artifacts/articles/wechat/8HYZMtpJi892jYm0U_5TSA/metadata.json \
  ../../../GX-harness/artifacts/articles/xiaohongshu/xhs-20261111-205014-c4b3a5-S2/6ada02c3000000002a0386c8/metadata.json
```
