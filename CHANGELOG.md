# Changelog

本文件记录已经进入代码库的用户可见能力。未来计划单独记录在 [`ROADMAP.md`](ROADMAP.md)。

## 0.4.0

- 提供统一的新用户安装、自检、本机凭证录入和首次同步流程；
- 提供数据质量、血缘、当前问题和恢复状态内核；
- 提供 Orders 2026 增量同步与断点恢复；
- 提供当前 FBA 库存完整快照同步；
- 提供 Sponsored Products Campaign、搜索词和购买商品报表同步；
- 提供已闭账结算报告同步与金额对账；
- 提供九个只读 MCP 查询工具；
- 提供通用 Agent Skill、诊断命令和 MCP 握手验证。

每项连接器的已验证行为与范围限制见 [`docs/connectors.md`](docs/connectors.md)及相应验证文档。这里的“已实现”不代表用户已经自动获得 Amazon 应用授权或其账户具备所有 API 角色。
