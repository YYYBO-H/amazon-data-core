# Amazon Data Core

把 Amazon 订单、库存、广告和结算数据同步到本地，并先验证数据是否完整、及时、可追溯，再交给你选择的 AI 或分析工具使用。

Amazon Data Core 是本地数据引擎和只读 MCP Server，**不是广告优化 Agent，也不是聊天界面**。它负责提供可信的经营事实，不替用户或模型做主观经营决策。

## 适合谁

- 没有研发团队，希望让现有 Agent 使用自己真实店铺数据的 Amazon 卖家和运营人员；
- 需要统一数据口径、质量检查和本地存储的服务商与技术团队；
- 希望更换 AI 工具时，不必重新同步和解释全部数据的用户。

## 当前支持

| 数据集 | 当前能力 | 重要范围 |
| --- | --- | --- |
| 订单 | SP-API Orders 2026 增量同步、分页恢复、原始与标准化数据 | 不请求买家或收件人信息 |
| 库存 | 当前 FBA 完整快照、版本切换、血缘 | 不包含 FBM/MFN，也不是历史库存 |
| 广告 | Sponsored Products Campaign、搜索词、购买商品报表 | 使用独立 Ads 授权；不同粒度不可相加 |
| 结算 | 已闭账结算报告、金额解析与逐行对账 | 不等于实时收入或利润 |

FBM/MFN 库存、Sponsored Brands/Display、Targeting、Ads Unified Reporting 和 Finances v2024 实时流水尚未支持。完整边界见[项目范围](docs/project-scope.md)和[路线图](ROADMAP.md)。

## 交给 Agent 安装

前提是 Agent 能读取 GitHub 仓库并在你的电脑上执行 Shell 命令。把仓库地址和下面这句话交给它：

```text
请克隆这个仓库，并严格按照 AGENT_INSTALL.md 运行现有的 Amazon Data Core。
不要重新开发应用或生成前端。需要 Amazon 凭证时把终端交给我输入，不要让我把凭证发送到聊天中。
```

Agent 专用的完整执行契约见 [`AGENT_INSTALL.md`](AGENT_INSTALL.md)。不具备本机终端能力的聊天产品不能代替你安装；这时请使用下面的终端入口。

## 在终端安装

需要 Git、Docker Desktop 和 Docker Compose：

```bash
git clone https://github.com/yanghanson801-rgb/amazon-data-core.git
cd amazon-data-core
./scripts/onboard.sh
```

`onboard.sh` 会安装服务、执行数据库迁移和自检，然后在本机终端引导 Amazon 授权与首次同步。凭证只写入权限为 `0600` 且已被 Git 忽略的本机 `.env`；不要把 Client Secret 或 Refresh Token 发给任何聊天模型。

只安装空白 Core、暂不连接 Amazon：

```bash
./scripts/install.sh
```

首次连接真实店铺前，你仍需拥有自己组织的 Amazon private SP-API 应用并完成自授权。广告数据还需要单独的 Amazon Ads 应用授权和 Profile ID。开源代码不能绕过 Amazon 审核、OAuth 同意或所需角色，详见 [Amazon 授权说明](docs/amazon-authorization.md)。

## 如何确认安装完成

完整的新用户流程只有在终端最终显示 `First sync passed` 时才算完成。如果某个数据集失败，脚本会列出失败项并返回非零退出码，不会把“部分成功”描述成“全部完成”。

空白 Core 可以用以下命令验证：

```bash
curl --fail http://localhost:8080/health
docker compose exec -T core amazon-data-core doctor
docker compose exec -T core python scripts/verify_mcp.py
docker compose exec -T core amazon-data-core status
```

本地服务启动后提供：

- 状态页：<http://localhost:8080>
- API 文档：<http://localhost:8080/docs>
- 数据健康：<http://localhost:8080/v1/data-health>
- 只读 MCP Server：`amazon-data-core mcp`

## 数据为什么可以核验

每次同步都会保留数据来源、店铺、站点、业务日期、抓取时间、源更新时间、原始引用和处理版本，并明确记录：

- 同步是否完整，源记录数与标准化、去重数量是否相符；
- 数据是否过期、仍在归因窗口内或后来被 Amazon 修正；
- 迟到数据是否比当前版本更新，是否可以切换为当前事实；
- 检查是 `passed`、`failed`、`skipped` 还是 `error`；
- 当前问题是否仍然开放，以及后续成功是否已经关闭旧问题。

缺失数据不会被解释为零销售、零广告或零库存。经营数据摘要类 MCP 查询会同时返回相应的数据覆盖范围和 `safe_to_analyze` 判断。

## 文档

- [项目范围与非目标](docs/project-scope.md)
- [连接器、同步命令与数据口径](docs/connectors.md)
- [Amazon 授权说明](docs/amazon-authorization.md)
- [Agent 安装契约](AGENT_INSTALL.md)
- [版本记录](CHANGELOG.md)
- [路线图](ROADMAP.md)
- [FBA 库存验证](docs/fba-inventory-validation.md)
- [广告 Campaign 验证](docs/ads-campaign-validation.md)
- [广告明细验证](docs/ads-detail-validation.md)
- [结算验证](docs/settlement-validation.md)

通用 Skill 位于 [`skills/amazon-data-core/SKILL.md`](skills/amazon-data-core/SKILL.md)，可以通过 `amazon-data-core install-skill --host generic` 安装。不同 Agent 的 MCP 配置入口可能不同；项目提供标准 MCP stdio 服务，但不声称所有产品都使用同一个配置文件或都具备本机执行权限。

## 开发验证

```bash
python3 -m pytest
```

许可证：[Apache-2.0](LICENSE)
