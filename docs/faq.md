# 常见问题

## Amazon Data Core 是做什么的？

它把卖家授权的 Amazon 订单、当前 FBA 库存、Sponsored Products 报表和已闭账结算
同步到本机 PostgreSQL，先检查数据是否完整、及时和可对账，再通过九个只读 MCP 工具
提供给兼容的本地 Agent 或分析工具。

## 我完全不懂技术，应该从哪里开始？

把官方仓库地址 <https://github.com/YYYBO-H/amazon-data-core> 交给具备本机执行能力的
Agent，并让它帮助安装和引导配置。Agent 会读取根目录 `AGENTS.md`，先检查电脑环境，
再打开本机配置页面。你不需要复制一长段命令或提示词。

## 我的 Amazon 数据会上传到别人的服务器吗？

默认不会。数据库和状态页面运行在本机，Docker 端口只绑定 `127.0.0.1`。如果使用者
自行改成远程数据库、远程主机或公网代理，则需要自行承担相应安全与合规责任。

## Amazon 凭证应该发给 Agent 吗？

不应该。凭证应由用户在临时 localhost 页面中亲自填写，只保存到权限为 `0600`、已被
Git 忽略的本机 `.env`。不要把 Secret、Refresh Token 或 `.env` 粘贴到聊天、Issue
或公开日志。

## 项目会替我申请 Amazon API 吗？

不会。用户仍需拥有自己的 private SP-API 应用并完成自授权。Amazon Ads 数据还需要
独立的 Ads API 授权和 Profile ID。项目不能绕过 Amazon 的审核、角色或同意流程。

## 第一次同步会很慢吗？

订单和当前库存通常按同步 API 分页返回；Ads 与结算使用异步报表，Amazon 生成报表时
可能需要等待。首次历史导入通常比后续增量同步慢。页面只展示数据库记录的真实状态，
不会用计时器伪造进度。

## 哪些电脑可以安装？

目前验证了 macOS、Linux 和 Windows 中的 WSL2。原生 Windows PowerShell 尚未验证，
预检工具会明确建议使用 WSL2。Core 运行在 Docker 中，启动脚本需要 Python 3.9 以上。

## Agent 什么时候可以开始分析？

只有必选数据集已经到达、质量检查通过且 `safe_to_analyze=true` 时。缺失数据不能被
解释为零销售、零库存、零广告或零回款。

## 它能计算准确利润吗？

当前不能直接提供完整准确利润。已闭账结算代表现金流，不等同于订单日期收入或利润；
当前版本也不包含完整采购成本、税务和 Finances v2024 实时流水。

## 它会自动修改广告吗？

不会。MCP 工具均为只读，项目有意不提供自动调价、改广告、改库存或其他 Amazon 写操作。

## 它是 Amazon 官方项目吗？

不是。Amazon Data Core 是由 LGURT 维护的独立开源项目，官方仓库为
[`YYYBO-H/amazon-data-core`](https://github.com/YYYBO-H/amazon-data-core)，与 Amazon
没有隶属、官方背书或代理关系。

## 什么情况下不应该选择它？

如果需要托管 SaaS、FBM/MFN 库存、Sponsored Brands/Display、实时完整利润、多店铺
成熟生产托管或自动广告执行，当前版本不适合。完整判断见[选择指南](decision-guide.md)。
