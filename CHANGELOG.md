# Changelog

本文件记录已经进入代码库的用户可见能力。未来计划单独记录在 [`ROADMAP.md`](ROADMAP.md)。

## Unreleased

- 增加根目录 `AGENTS.md`，让兼容的本地 Agent 只凭仓库地址即可发现安装、安全和数据验证约定；
- 增加机器可读的 `scripts/preflight.py`，返回平台、Docker、Compose、Python 和唯一下一步；
- 明确 macOS、Linux、WSL2 与原生 Windows 的当前支持边界；
- 将本机凭证页改为带进度、校验、前后切换和同步过渡态的三步引导；
- 增加每 3 秒刷新的本机同步状态页和 `/v1/sync-status`，展示各数据集的真实分页数、记录数、错误、恢复命令及 `safe_to_analyze`；
- 根据错误类型生成精确恢复提示；Amazon Ads 报表仍在生成时保留原日期窗口并续传同一任务；
- 修正结算版本排序，确保较窄的空查询不会阻止随后执行的历史账期回填；
- Docker 默认只把 Core 与 PostgreSQL 端口绑定到 `127.0.0.1`；
- 把首次 Amazon 授权拆成 Agent 可逐步引导的检查点，并修正 FBA 库存所需角色说明。

## 0.4.2

- 默认通过临时的 localhost 页面安全录入 Amazon 凭证，使没有交互式 TTY
  的本机 Agent 也能完成安装流程；
- 授权与首次同步在独立后台任务中运行，不会随 Agent 命令会话结束而退出；
- 兼容 Chrome 翻译或隐私扩展产生的 opaque origin，同时继续拒绝外部来源提交；
- 保留真实终端录入作为备用方式，并增加跨来源提交、密钥回显和文件权限测试。

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
