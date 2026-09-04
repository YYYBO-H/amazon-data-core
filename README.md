# Amazon Data Core

面向 Amazon 卖家系统的可信经营数据底座。它不替商家决定“广告应该怎么调”，而是先回答更基础也更关键的问题：**今天拿到的订单、库存、广告和结算数据，是否完整、及时、顺序正确，能不能用于经营判断？**

```bash
git clone https://github.com/yanghanson801-rgb/amazon-data-core.git
cd amazon-data-core
./scripts/onboard.sh
```

它的核心契约是：

- 每批数据保留来源、店铺、站点、业务日期、抓取时间和源更新时间；
- 同时保留时区、币种、原始报表引用、Schema/公式版本、暂定与修正关系；
- 新版本优先，迟到的旧快照留痕但不能覆盖当前数据；
- 检查结果明确区分 `passed / failed / skipped / error`；
- 检查事件只追加，当前问题由最新有效判断推导；
- 后续通过会自动关闭旧问题，但不会删除历史；
- 经营规则可以配置，数据真实性契约保持稳定。

产品定位、目标用户与明确的非目标见
[`docs/product-contract.md`](docs/product-contract.md)。这份契约用于防止项目重新漂移成
聊天机器人、广告建议 Agent 或只会导入报表的脚本。

## 给 Agent 一句话安装并接入真实店铺

把仓库地址交给任何能执行 Shell 的 Agent，并告诉它：

```text
请按照仓库里的 AGENT_INSTALL.md，在本机安装 Amazon Data Core，并引导我在终端完成 Amazon 自授权和首次同步。不要让我把凭证发到聊天里。
```

Agent 会运行统一入口，并在需要秘密时把终端交还给用户输入：

```bash
./scripts/onboard.sh
```

它会依次完成 Docker 环境验证、服务安装、数据库迁移、`doctor`、九个 MCP 工具验证、
本机凭证安全录入、LWA 授权验证、订单/FBA 库存/结算首次同步，以及可选的 Amazon Ads
授权与三类广告报表同步。凭证由用户直接输入终端，只写入 Git 已忽略且权限为 `0600`
的本机 `.env`，不写入数据库、日志或 Agent 对话。

首次接入前，卖家仍需在 Amazon 官方后台注册 private SP-API 应用并完成自授权；开源代码
不能绕过 Amazon 的开发者审核或代替用户同意授权。完整准备步骤和 public OAuth 边界见
[`docs/amazon-authorization.md`](docs/amazon-authorization.md)。

只想先安装空白 Core、不配置店铺，可以运行：

```bash
./scripts/install.sh
```

## 一键运行

需要 Docker：

```bash
docker compose up --build
```

启动完成后访问：

- 状态页：http://localhost:8080
- API 文档：http://localhost:8080/docs
- 数据健康：http://localhost:8080/v1/data-health

容器会自动建库并执行检查，默认不会把演示数据混入真实数据库。仅在评估演示时显式运行：

```bash
LOAD_DEMO=true docker compose up --build
```

## 接入真实数据

任何 Amazon SP-API、Ads API、报表下载器或现有 ETL，只要把一次同步结果提交为统一的 `dataset run`：

```bash
curl -X POST http://localhost:8080/v1/runs \
  -H 'content-type: application/json' \
  -d '{
    "source":"amazon_sp_api",
    "store_id":"store-us-1",
    "marketplace":"ATVPDKIKX0DER",
    "dataset":"inventory",
    "business_date":"2026-09-03",
    "fetched_at":"2026-09-03T03:10:00Z",
    "source_updated_at":"2026-09-03T03:00:00Z",
    "ingestion_status":"complete",
    "source_count":120,
    "normalized_count":118,
    "duplicate_count":2
  }'
```

然后执行 `POST /v1/checks/run`。Core 负责证明数据是否可信，连接器可以替换。

### Amazon Orders 直连（v0.3）

推荐使用交互式配置器填写 Amazon LWA 应用凭证、卖家授权产生的 refresh token、
店铺代号、Marketplace ID、区域、时区和币种。不要把真实值发送给模型或提交到仓库。

```bash
python3 scripts/configure.py
./scripts/sync-all.sh
```

`configure.py` 对常用站点自动填写 Marketplace ID、区域、IANA 时区和币种；
`sync-all.sh` 会先验证授权，再分别同步各数据集。一个数据集失败不会删除已经成功的数据，
最后会明确列出失败项并返回非零退出码，避免把“部分成功”误报成“全部完成”。

连接器调用当前 `/orders/2026-01-01/orders` 接口，默认只请求 `FULFILLMENT` 和
`PROCEEDS`，明确拒绝 `BUYER`/`RECIPIENT`。它将脱敏后的原始响应和标准化订单分别
落到本地 PostgreSQL，保存同步窗口与分页断点，并对限流、临时错误、重复数据和迟到
更新做处理。失败后再次执行同一命令会从已提交的分页断点继续。按业务日期查询时，
只有完整日期落在已成功回填到当前游标的连续覆盖区间内，MCP 才会返回
`safe_to_analyze=true`；最新增量批次为 0 行并不代表历史日期为 0 单。

### Amazon FBA 库存直连（v0.3）

订单直连验证成功后，使用同一组卖家授权凭证拉取当前 FBA 库存：

```bash
docker compose exec -T core amazon-data-core sync-inventory
```

连接器调用 `/fba/inventory/v1/summaries`，每次获取完整的当前快照；分页结果全部取回
后才写入并切换当前版本。它按 `sellerSku + fnSku + condition` 保存库存记录，因此同一
ASIN 的多个库存行不会互相覆盖；只有无坏行的完整快照才会停用本次未出现的旧行。
分页令牌过期时整批重拉，原始版本、快照成员和标准化当前值均保留血缘。

这个命令只包含 Amazon 配送网络中的 **FBA 库存**，不包含 FBM/MFN 自配送库存，
也不是历史某日的库存。MCP 会固定返回这项范围警告，避免 Agent 把缺失的 FBM 数量
误报为零库存。

### Amazon Ads Campaign 直连（v0.4 过渡版）

Amazon Ads 使用独立的应用授权和广告 Profile。填写 `.env` 中的
`AMAZON_AD_CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN / PROFILE_ID` 后执行：

```bash
docker compose exec -T core amazon-data-core amazon-ads-auth --verify
docker compose exec -T core amazon-data-core sync-ads-campaigns
docker compose exec -T core amazon-data-core sync-ads-search-terms
docker compose exec -T core amazon-data-core sync-ads-purchased-products
```

默认拉取广告账户时区的前一天，以 `日期 + campaignId` 保存 Sponsored Products
Campaign 日报的曝光、点击、花费及 1/7/14 天点击归因销售、购买和件数。报告请求、
轮询状态、下载原文、标准化版本和日期覆盖均可追溯；异步报告未完成时重跑会继续同一
reportId。最近 14 天归因明确标记为仍可修正，归因销售不等于店铺总销售或利润。
同步前还会读取 Ads Profile，并校验 Profile ID、Marketplace、币种和 IANA 时区；
任一项与本地店铺配置冲突都会停止写入，避免把正确数据归到错误店铺或错误币种。

当前真实连接器使用仍可生产验证的 Reporting v3，但本地 Canonical Schema 与 API
版本解耦。Amazon 已宣布旧 Sponsored Ads/DSP 报表将在 2026-12-31 退出，而新的
Unified Reporting API 仍处于 beta；因此 MCP 固定提示迁移边界，后续 Unified
适配器可以写入同一份本地事实模型，而不要求 Agent 改查询方式。

搜索词与购买商品是两份互补但不可相加的报表：

- `sync-ads-search-terms` 保存用户真实搜索词、曝光、点击、花费和 1/7/14 天归因结果，
  但 Amazon 不在这份报表返回 ASIN/SKU；
- `sync-ads-purchased-products` 保存广告 ASIN 到实际购买 ASIN 的 1/7/14/30 天归因，
  但不返回曝光、点击或花费，不能单独计算 ACOS。

两类非 Campaign 粒度的 v3 报表不接受 `campaignStatus` 过滤器，因此请求不伪造
“包含全部 Archived Campaign”的承诺；数据集元数据与 MCP 都会固定披露 Amazon
默认 eligibility 范围。它们同样保留每次报表版本，并标记尚在归因窗口内的数据。

### Amazon 结算直连（v0.4）

结算使用与订单、库存相同的 SP-API 卖家授权，但应用必须具有 Finance and
Accounting 角色：

```bash
docker compose exec -T core amazon-data-core sync-settlements
```

连接器通过 Reports API 查找 Amazon 自动生成的
`GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2`，逐页下载仍可获取的闭账报表。结算报告
不能按需创建，因此命令同步的是已经关闭并由 Amazon 生成的结算期，不代表今天的订单
收入。每份文件保留原始字段、报告 ID、文档校验和及标准化版本；只有恰好一个汇总行、
所有明细金额有效，并且“逐行金额合计与净打款差额不超过 0.01”时才发布为当前结算。
错误的新报告不会覆盖之前正确的结算版本。

Amazon 官方说明金额使用本地格式，因此解析器同时处理 `1,234.56`、`1.234,56` 和
`95,00`，无法消除歧义的值会进入拒绝记录。Core 不把 Amazon 的原始
`transaction-type / amount-type / amount-description` 强行变成固定经营分类；MCP
返回原始维度和精确对账，让上层按可版本化规则解释。配置里的 Marketplace 是卖家连接
范围，一份结算仍可能包含多个 marketplace name，不能据此把每行强行归到一个站点。

连接器的契约与隐私安全验证见：

- [FBA 库存的快照、分页和粒度验证](docs/fba-inventory-validation.md)
- [Sponsored Products Campaign 的异步恢复与归因修订验证](docs/ads-campaign-validation.md)
- [搜索词与购买商品的主键和范围限制验证](docs/ads-detail-validation.md)
- [结算报告的格式兼容和精确对账验证](docs/settlement-validation.md)

## 当前内置检查

- `freshness`：最新数据是否超过允许延迟（可按来源、店铺和站点分别配置）；
- `completeness`：本次同步是否明确完整；
- `reconciliation`：源记录数是否与标准化记录数和去重数对得上；
- `ordering`：最近一次到达的数据是否因版本较旧被拒绝。

规则按数据集配置在数据库中，可以通过 API 增加或停用。

## 让任意 Agent 使用

v0.4 提供只读 MCP Server，先让 Agent 判断数据能不能用，再读取本地事实：

```bash
amazon-data-core mcp
```

它暴露九个稳定工具：

- `amazon_data_health`：总体数据健康与检查结果；
- `amazon_dataset_status`：各店铺数据源、日期、数量、版本与暂定状态；
- `amazon_data_issues`：仍未解决的数据质量问题。
- `amazon_orders_summary`：指定店铺和业务日期的订单数、件数、可用卖家收入、履约状态，
  并同时返回数据是否可安全分析。
- `amazon_fba_inventory_status`：当前 FBA 库存数量分层、低可售库存行、数据范围和
  是否可安全分析；明确排除 FBM/MFN。
- `amazon_ads_campaign_summary`：指定日期范围的 Sponsored Products Campaign
  曝光、点击、花费和归因结果，同时返回覆盖范围、暂定状态和数据质量结论。
- `amazon_ads_search_term_summary`：用户搜索词、曝光、点击、花费及归因结果；明确
  不含 ASIN/SKU。
- `amazon_ads_purchased_product_summary`：广告 ASIN 到购买 ASIN 的归因结果；明确
  不含曝光、点击或花费，不能单独计算 ACOS。
- `amazon_settlement_summary`：按结算结束日或打款日查询闭账周期、净打款、逐行金额
  对账和 Amazon 原始费用维度；明确不等同于按下单日统计的收入或利润。

通用 Skill 位于 [`skills/amazon-data-core/SKILL.md`](skills/amazon-data-core/SKILL.md)，
也可以通过 `amazon-data-core install-skill --host generic` 安装。MCP 的 Docker
配置示例见 [`AGENT_INSTALL.md`](AGENT_INSTALL.md)。

项目目前已包含订单、FBA 库存、Sponsored Products Campaign、搜索词、购买商品
日报以及闭账结算切片：代码包含 Orders 2026 API、FBA Inventory API、Ads Reporting
v3、Reports API、原始/标准化双层存储、增量
水位或完整快照、异步恢复、质量闭环和对应 MCP 查询。仓库不会
伪装成已替用户获得 Amazon 官方应用授权；真实店铺验证需要用户自己的授权凭证。
FBM/MFN 库存、Sponsored Brands/Display、Targeting 粒度和实时 Finances v2024
交易流水仍未完成。
