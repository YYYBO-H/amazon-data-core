# 连接器与数据口径

本文集中说明真实数据连接器的运行方式和数据含义。安装入口仍然是仓库根目录的 `./scripts/onboard.sh`。

## 统一同步

重新打开本机配置页并在提交后自动同步全部已配置的数据集：

```bash
python3 scripts/onboard_background.py launch
```

配置页只监听本机 `127.0.0.1`，提交后即关闭。没有浏览器时可改用
`./scripts/onboard.sh --terminal-config` 在真实终端中录入。
默认新用户流程会把授权服务和随后的首次同步放进独立后台任务，避免它们随
Agent 的临时命令会话退出。运行 `python3 scripts/onboard_background.py status`
可查看当前是等待授权、同步中、完成还是失败。

一个数据集失败不会删除其他已经成功的数据，但命令会列出失败项并返回非零退出码。所有连接器都保留原始响应或原始报表、标准化版本、同步批次和质量结果。

## Orders

Orders 连接器使用当前 SP-API Orders 2026 接口，默认只请求 `FULFILLMENT` 和 `PROCEEDS`，拒绝 `BUYER` 和 `RECIPIENT` 数据。它支持分页、限流、临时错误重试和已提交分页断点恢复。

按业务日期查询时，只有目标日期处于连续成功覆盖范围内，MCP 才会返回 `safe_to_analyze=true`。最新增量批次为零行，不代表历史日期的订单数为零。

## FBA 库存

```bash
docker compose exec -T core amazon-data-core sync-inventory
```

连接器从 `/fba/inventory/v1/summaries` 获取完整的当前快照。所有分页成功后才切换当前版本；分页令牌过期会触发整批重拉。库存按 `sellerSku + fnSku + condition` 保存，避免同一 ASIN 的多行库存互相覆盖。

该数据只包含 Amazon 配送网络中的 FBA 当前库存，不包含 FBM/MFN，也不是历史某日库存。详细验证见 [FBA 库存验证](fba-inventory-validation.md)。

## Amazon Ads

Ads 使用独立的应用授权和广告 Profile：

```bash
docker compose exec -T core amazon-data-core amazon-ads-auth --verify
docker compose exec -T core amazon-data-core sync-ads-campaigns
docker compose exec -T core amazon-data-core sync-ads-search-terms
docker compose exec -T core amazon-data-core sync-ads-purchased-products
```

同步前会校验 Profile ID、Marketplace、币种和 IANA 时区。异步报告未完成时，重跑会继续已有 report ID；仍在归因窗口内的数据会标记为可修正。

三个报表的粒度不同：

- Campaign 报表包含曝光、点击、花费和 1/7/14 天点击归因结果；
- 搜索词报表包含真实搜索词及相应表现，但不返回 ASIN/SKU；
- 购买商品报表包含广告 ASIN 到实际购买 ASIN 的归因结果，但不返回曝光、点击或花费。

这些报表互补但不可相加，购买商品报表也不能单独计算 ACOS。广告归因销售不等于店铺总销售或利润。当前连接器使用 Reporting v3，并通过独立的本地 Canonical Schema 隔离上游 API 变化。详细验证见 [Campaign 验证](ads-campaign-validation.md)和[广告明细验证](ads-detail-validation.md)。

## 结算

```bash
docker compose exec -T core amazon-data-core sync-settlements
```

结算连接器需要 SP-API 应用具备 Finance and Accounting 角色。它通过 Reports API 下载 Amazon 已生成的 `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2` 闭账报表；这不是实时交易流水，也不等于按下单日统计的收入或利润。

每份文件保留报告 ID、原始字段、文档校验和和标准化版本。只有汇总结构有效、所有明细金额可解析，且逐行金额合计与净打款差额不超过 `0.01` 时，版本才会发布为当前结算。错误的新报告不会覆盖先前正确版本。

解析器支持常见本地金额格式；无法消除歧义的值进入拒绝记录。Amazon 原始 `transaction-type`、`amount-type` 和 `amount-description` 会保留，不会被静默映射成固定经营分类。详细验证见 [结算验证](settlement-validation.md)。

## 自定义数据入口

已有 SP-API 下载器或 ETL 的团队，可以把一次同步结果提交为统一的 dataset run，再执行质量检查：

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

随后调用 `POST /v1/checks/run`。自定义连接器可以替换，但必须遵守相同的数据集、血缘和质量契约。

## 只读 MCP 查询

运行：

```bash
amazon-data-core mcp
```

当前暴露九个只读工具：

- `amazon_data_health`
- `amazon_dataset_status`
- `amazon_data_issues`
- `amazon_orders_summary`
- `amazon_fba_inventory_status`
- `amazon_ads_campaign_summary`
- `amazon_ads_search_term_summary`
- `amazon_ads_purchased_product_summary`
- `amazon_settlement_summary`

查询结果携带数据覆盖、质量状态和相应范围警告，供上层 Agent 决定是否可以继续分析。
