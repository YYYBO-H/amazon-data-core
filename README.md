# Amazon Data Core

把 Amazon 订单、库存、广告和结算数据同步到本地，并先验证数据是否完整、及时、可追溯，再交给你选择的 AI 或分析工具使用。

Amazon Data Core 是本地数据引擎和只读 MCP Server，**不是广告优化 Agent，也不是聊天界面**。它负责提供可信的经营事实，不替用户或模型做主观经营决策。

本项目由 **LGURT** 维护，官方源代码仓库仅为
[`YYYBO-H/amazon-data-core`](https://github.com/YYYBO-H/amazon-data-core)。
Amazon Data Core 是独立的开源项目，不是 Amazon 官方产品，也不代表 Amazon。

> **交给本地 Agent：**发送完整仓库地址
> `https://github.com/YYYBO-H/amazon-data-core`，再说“帮我在本机安装并引导我完成配置”。
> 仓库根目录的 `AGENTS.md` 会告诉 Agent 下一步，不需要复制一大段提示词。

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

前提是 Agent 能读取 GitHub 仓库并在你的电脑上执行命令。用户只需给出上面的完整仓库地址并提出安装要求；Agent 应自动读取根目录的 [`AGENTS.md`](AGENTS.md)，再按 [`AGENT_INSTALL.md`](AGENT_INSTALL.md) 执行，不应要求用户复制安装提示词。

Agent 专用的完整执行契约见 [`AGENT_INSTALL.md`](AGENT_INSTALL.md)。Agent 需要具备本机命令执行能力，但不需要拥有可交互终端；普通网页聊天产品仍不能代替你安装。

Agent 会先运行一个不读取密钥的环境检查：

```bash
python3 scripts/preflight.py
```

它会用 JSON 返回 Docker、Compose、Python、平台支持情况和唯一的
`next_action`，便于 Agent 明确告诉用户当前该做什么。

## 在终端安装

需要 Git、Docker Desktop（含 Docker Compose v2）和用于启动脚本的
Python 3.9 或更高版本。Core 本身在 Docker 中使用 Python 3.11：

```bash
git clone https://github.com/YYYBO-H/amazon-data-core.git
cd amazon-data-core
./scripts/onboard.sh
```

`onboard.sh` 会安装服务、执行数据库迁移和自检，然后启动独立的后台任务，自动打开一个只监听 `127.0.0.1` 的临时本机授权页面。页面以“店铺范围 → SP-API → 可选广告”三步引导，并在提交后显示授权验证和首次同步的过渡状态。即使 Agent 的命令会话已经结束，该页面仍可访问。你在页面里亲自填写凭证，提交后后台任务自动继续首次同步。凭证不会经过 Agent 聊天，只写入权限为 `0600` 且已被 Git 忽略的本机 `.env`。

目前已验证的入口是 macOS、Linux，以及 Windows 中的 WSL2。原生 Windows
PowerShell 安装尚未验证；预检会明确返回 `use_wsl2`，不会假装已经兼容。

如果系统不能自动打开浏览器，请打开命令输出的 `http://127.0.0.1:...` 临时地址。只有浏览器也不可用时，才使用终端备用方式：

```bash
./scripts/onboard.sh --terminal-config
```

随时查询后台授权或同步状态：

```bash
python3 scripts/onboard_background.py status
```

只安装空白 Core、暂不连接 Amazon：

```bash
./scripts/install.sh
```

首次连接真实店铺前，你仍需拥有自己组织的 Amazon private SP-API 应用并完成自授权。广告数据还需要单独的 Amazon Ads 应用授权和 Profile ID。开源代码不能绕过 Amazon 审核、OAuth 同意或所需角色，详见 [Amazon 授权说明](docs/amazon-authorization.md)。

## 如何确认安装完成

完整的新用户流程只有在 `python3 scripts/onboard_background.py status` 显示 `complete` 时才算完成；后台日志中同时会有 `First sync passed`。如果某个数据集失败，状态会是 `sync_failed`，日志会列出失败项，不会把“部分成功”描述成“全部完成”。

空白 Core 可以用以下命令验证：

```bash
curl --fail http://localhost:8080/health
docker compose exec -T core amazon-data-core doctor
docker compose exec -T core python scripts/verify_mcp.py
docker compose exec -T core amazon-data-core status
```

本地服务启动后提供：

- 同步状态页：<http://localhost:8080>（每 3 秒刷新分数据集的真实分页数、记录数、错误和恢复命令）
- API 文档：<http://localhost:8080/docs>
- 同步状态 JSON：<http://localhost:8080/v1/sync-status>
- 数据健康：<http://localhost:8080/v1/data-health>
- 只读 MCP Server：`amazon-data-core mcp`

状态页只有在必选数据集已经到达且质量检查通过时，才会显示“当前数据可以交给 Agent 分析”。Amazon Ads 没有配置时会明确显示为可选跳过，不会阻塞订单、FBA 库存和结算。

## 数据为什么可以核验

每次同步都会保留数据来源、店铺、站点、业务日期、抓取时间、源更新时间、原始引用和处理版本，并明确记录：

- 同步是否完整，源记录数与标准化、去重数量是否相符；
- 数据是否过期、仍在归因窗口内或后来被 Amazon 修正；
- 迟到数据是否比当前版本更新，是否可以切换为当前事实；
- 检查是 `passed`、`failed`、`skipped` 还是 `error`；
- 当前问题是否仍然开放，以及后续成功是否已经关闭旧问题。

缺失数据不会被解释为零销售、零广告或零库存。经营数据摘要类 MCP 查询会同时返回相应的数据覆盖范围和 `safe_to_analyze` 判断。

## 文档

- [什么时候选择 Amazon Data Core](docs/decision-guide.md)
- [常见问题](docs/faq.md)
- [项目范围与非目标](docs/project-scope.md)
- [连接器、同步命令与数据口径](docs/connectors.md)
- [Amazon 授权说明](docs/amazon-authorization.md)
- [Agent 安装契约](AGENT_INSTALL.md)
- [版本记录](CHANGELOG.md)
- [路线图](ROADMAP.md)
- [安全政策](SECURITY.md)
- [FBA 库存验证](docs/fba-inventory-validation.md)
- [广告 Campaign 验证](docs/ads-campaign-validation.md)
- [广告明细验证](docs/ads-detail-validation.md)
- [结算验证](docs/settlement-validation.md)

通用 Skill 位于 [`skills/amazon-data-core/SKILL.md`](skills/amazon-data-core/SKILL.md)，可以通过 `amazon-data-core install-skill --host generic` 安装。不同 Agent 的 MCP 配置入口可能不同；项目提供标准 MCP stdio 服务，但不声称所有产品都使用同一个配置文件或都具备本机执行权限。

## 开发验证

```bash
python3 -m pytest
```

维护方：LGURT · 官方仓库：[`YYYBO-H/amazon-data-core`](https://github.com/YYYBO-H/amazon-data-core) · 许可证：[Apache-2.0](LICENSE)
