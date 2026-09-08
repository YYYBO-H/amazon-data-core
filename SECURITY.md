# 安全政策 / Security Policy

Amazon Data Core 会接触 Amazon API 凭证和卖家经营数据，因此安全问题请通过
私密渠道报告，不要在公开 Issue、Discussion、截图、日志或 Agent 聊天中粘贴凭证。

## 支持范围

安全修复优先进入当前 `main` 和最新 GitHub Release。旧版本不保证单独回补；报告问题
时请说明使用的版本、操作系统、安装方式和不包含经营数据的最小复现步骤。

## 私密报告漏洞

请使用 GitHub 的
[Private vulnerability reporting](https://github.com/YYYBO-H/amazon-data-core/security/advisories/new)
提交报告。请只提供定位问题所需的信息，并先移除以下内容：

- Amazon Client Secret、Refresh Token、Access Token 和 Ads Token；
- Seller ID、Profile ID、订单号、SKU、ASIN 及结算金额；
- `.env` 内容、数据库备份和完整生产日志；
- 生产服务器地址、个人邮箱及其他账户信息。

如果凭证已经出现在公开位置，请立即在相应 Amazon 控制台撤销或轮换；删除帖子或
Git 历史不能让已经泄露的凭证重新安全。

## 安全边界

- 配置页面只监听 `127.0.0.1`，凭证写入 Git 忽略且权限为 `0600` 的本机 `.env`；
- Amazon 经营数据默认写入本机 PostgreSQL；
- MCP 工具为只读，不包含广告修改或其他 Amazon 写操作；
- 项目不会要求用户在 Agent 聊天中提交 Amazon 凭证。

这些边界不等于完整的生产安全审计。将项目接入多用户服务器、远程数据库或公网反向
代理前，使用者仍需自行评估访问控制、备份、主机安全和合规要求。
