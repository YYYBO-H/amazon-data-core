# Roadmap

以下是当前优先方向，不是发布日期承诺。实际交付以 [`CHANGELOG.md`](CHANGELOG.md) 和代码为准。

## 更简单的新用户接入

- 让用户只需把完整 GitHub 仓库地址交给本地 Agent，仓库即可自描述安装流程；
- 提供机器可读的环境预检、明确的下一步和可恢复的安装状态；
- 补齐原生 Windows PowerShell 入口、凭证文件 ACL 和 Windows CI；
- 为更多具备本机执行能力的 Agent 提供经过验证的 MCP 接入说明；
- 录制经过脱敏和日期标注的 Amazon 授权短视频与关键步骤截图；
- 让授权、同步和数据质量失败可以被普通运营人员理解和恢复。

## 更多数据覆盖

- FBM/MFN 自配送库存；
- Sponsored Brands、Sponsored Display 和 Targeting 粒度；
- Amazon Ads Unified Reporting 适配器；
- Finances v2024 实时交易流水；
- 订单、广告和结算之间的跨源对账。

## 多店铺运行

- 完善多店铺、站点、时区和币种的安装与隔离验证；
- 提供可验证的升级、备份和恢复流程。

路线图不会改变项目边界：Amazon Data Core 保持为本地可信数据层和只读查询服务，不发展为替用户执行经营决策的通用 Agent。
