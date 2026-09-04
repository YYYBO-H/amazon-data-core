CREATE TABLE IF NOT EXISTS data_scopes (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    dataset TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, store_id, marketplace, dataset)
);

CREATE TABLE IF NOT EXISTS quality_rules (
    id BIGSERIAL PRIMARY KEY,
    rule_code TEXT NOT NULL UNIQUE,
    check_type TEXT NOT NULL CHECK (
        check_type IN ('freshness', 'completeness', 'reconciliation', 'ordering')
    ),
    dataset TEXT NOT NULL,
    source TEXT,
    store_id TEXT,
    marketplace TEXT,
    severity TEXT NOT NULL DEFAULT 'warning' CHECK (
        severity IN ('info', 'warning', 'critical')
    ),
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE quality_rules
    ADD COLUMN IF NOT EXISTS source TEXT,
    ADD COLUMN IF NOT EXISTS store_id TEXT,
    ADD COLUMN IF NOT EXISTS marketplace TEXT;

CREATE TABLE IF NOT EXISTS dataset_runs (
    id UUID PRIMARY KEY,
    arrival_sequence BIGSERIAL UNIQUE,
    external_run_id TEXT,
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    dataset TEXT NOT NULL,
    business_date DATE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMPTZ,
    version_at TIMESTAMPTZ NOT NULL,
    ingestion_status TEXT NOT NULL CHECK (ingestion_status IN ('complete', 'partial')),
    source_count BIGINT NOT NULL CHECK (source_count >= 0),
    normalized_count BIGINT NOT NULL CHECK (normalized_count >= 0),
    duplicate_count BIGINT NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    error_count BIGINT NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    checksum TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    currency CHAR(3),
    raw_reference TEXT,
    schema_version TEXT NOT NULL DEFAULT '1',
    formula_version TEXT,
    is_provisional BOOLEAN NOT NULL DEFAULT FALSE,
    correction_of_run_id UUID REFERENCES dataset_runs(id),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    accepted BOOLEAN NOT NULL,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Monotonic arrival order is required because PostgreSQL NOW() is stable for
-- a whole transaction; two runs in one transaction can have identical times.
ALTER TABLE dataset_runs
    ADD COLUMN IF NOT EXISTS arrival_sequence BIGSERIAL,
    ADD COLUMN IF NOT EXISTS external_run_id TEXT,
    ADD COLUMN IF NOT EXISTS error_count BIGINT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_runs_arrival_sequence
    ON dataset_runs(arrival_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_runs_external_id
    ON dataset_runs(source, external_run_id)
    WHERE external_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dataset_runs_scope_version
    ON dataset_runs(source, store_id, marketplace, dataset, version_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS current_dataset_state (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    dataset TEXT NOT NULL,
    run_id UUID NOT NULL REFERENCES dataset_runs(id),
    business_date DATE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMPTZ,
    version_at TIMESTAMPTZ NOT NULL,
    ingestion_status TEXT NOT NULL,
    source_count BIGINT NOT NULL,
    normalized_count BIGINT NOT NULL,
    duplicate_count BIGINT NOT NULL,
    error_count BIGINT NOT NULL DEFAULT 0,
    checksum TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, dataset)
);

ALTER TABLE current_dataset_state
    ADD COLUMN IF NOT EXISTS error_count BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS quality_check_events (
    id BIGSERIAL PRIMARY KEY,
    rule_code TEXT NOT NULL REFERENCES quality_rules(rule_code),
    target_key TEXT NOT NULL,
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    dataset TEXT NOT NULL,
    target_date DATE,
    check_status TEXT NOT NULL CHECK (
        check_status IN ('passed', 'failed', 'skipped', 'error')
    ),
    severity TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quality_events_target
    ON quality_check_events(rule_code, target_key, evaluated_at DESC, id DESC);

CREATE OR REPLACE VIEW v_latest_checks AS
SELECT ranked.*
FROM (
    SELECT e.*, ROW_NUMBER() OVER (
        PARTITION BY e.rule_code, e.target_key
        ORDER BY e.evaluated_at DESC, e.id DESC
    ) AS state_rank
    FROM quality_check_events e
) ranked
WHERE ranked.state_rank = 1;

CREATE OR REPLACE VIEW v_open_issues AS
SELECT ranked.*
FROM (
    SELECT e.*, ROW_NUMBER() OVER (
        PARTITION BY e.rule_code, e.target_key
        ORDER BY e.evaluated_at DESC, e.id DESC
    ) AS state_rank
    FROM quality_check_events e
    WHERE e.check_status <> 'skipped'
) ranked
WHERE ranked.state_rank = 1
  AND ranked.check_status IN ('failed', 'error');

-- Recovery is not tied to currently-open issues: an earlier
-- failure/error is recovered when the latest non-skipped decision is passed.
CREATE OR REPLACE VIEW v_recovered_issues AS
SELECT DISTINCT history.rule_code, history.target_key
FROM quality_check_events history
JOIN (
    SELECT ranked.rule_code, ranked.target_key, ranked.check_status
    FROM (
        SELECT e.*, ROW_NUMBER() OVER (
            PARTITION BY e.rule_code, e.target_key
            ORDER BY e.evaluated_at DESC, e.id DESC
        ) AS state_rank
        FROM quality_check_events e
        WHERE e.check_status <> 'skipped'
    ) ranked
    WHERE ranked.state_rank = 1
) latest
  ON latest.rule_code = history.rule_code
 AND latest.target_key = history.target_key
WHERE history.check_status IN ('failed', 'error')
  AND latest.check_status = 'passed';

CREATE TABLE IF NOT EXISTS amazon_store_connections (
    id BIGSERIAL PRIMARY KEY,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    region TEXT NOT NULL CHECK (region IN ('NA', 'EU', 'FE')),
    timezone TEXT NOT NULL DEFAULT 'UTC',
    currency CHAR(3),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, marketplace)
);

CREATE TABLE IF NOT EXISTS amazon_sync_cursors (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    dataset TEXT NOT NULL,
    cursor_value TIMESTAMPTZ NOT NULL,
    last_attempt_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, dataset)
);

CREATE TABLE IF NOT EXISTS amazon_sync_attempts (
    id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    dataset TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'failed', 'finalizing', 'completed', 'partial')
    ),
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    pagination_token TEXT,
    pages_completed INTEGER NOT NULL DEFAULT 0,
    rows_pulled BIGINT NOT NULL DEFAULT 0,
    rows_inserted BIGINT NOT NULL DEFAULT 0,
    rows_updated BIGINT NOT NULL DEFAULT 0,
    rows_skipped BIGINT NOT NULL DEFAULT 0,
    rows_errored BIGINT NOT NULL DEFAULT 0,
    max_source_updated_at TIMESTAMPTZ,
    error_type TEXT,
    core_run_id UUID REFERENCES dataset_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_amazon_sync_attempt_scope
    ON amazon_sync_attempts(
        source, store_id, marketplace, dataset, created_at DESC
    );

CREATE TABLE IF NOT EXISTS amazon_orders_raw (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    order_id TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    pii_redacted BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        source, store_id, marketplace, order_id,
        source_updated_at, payload_checksum
    )
);

CREATE INDEX IF NOT EXISTS idx_amazon_orders_raw_scope_time
    ON amazon_orders_raw(store_id, marketplace, source_updated_at DESC);

CREATE TABLE IF NOT EXISTS amazon_order_rejects (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, payload_checksum, error_code)
);

CREATE TABLE IF NOT EXISTS amazon_orders (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    order_id TEXT NOT NULL,
    created_time TIMESTAMPTZ NOT NULL,
    last_updated_time TIMESTAMPTZ NOT NULL,
    fulfillment_status TEXT,
    fulfilled_by TEXT,
    proceeds_total_amount NUMERIC(20, 4),
    currency CHAR(3),
    item_count BIGINT NOT NULL DEFAULT 0,
    programs JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_raw_id BIGINT NOT NULL REFERENCES amazon_orders_raw(id),
    payload_checksum TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, order_id)
);

CREATE INDEX IF NOT EXISTS idx_amazon_orders_business_time
    ON amazon_orders(store_id, marketplace, created_time DESC);

ALTER TABLE amazon_orders
    ADD COLUMN IF NOT EXISTS proceeds_total_amount NUMERIC(20, 4);

CREATE TABLE IF NOT EXISTS amazon_fba_inventory_raw (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    inventory_key TEXT NOT NULL,
    asin TEXT NOT NULL,
    seller_sku TEXT NOT NULL,
    fn_sku TEXT,
    source_updated_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, store_id, marketplace, inventory_key, payload_checksum)
);

CREATE INDEX IF NOT EXISTS idx_amazon_fba_inventory_raw_scope
    ON amazon_fba_inventory_raw(store_id, marketplace, asin, seller_sku);

CREATE TABLE IF NOT EXISTS amazon_fba_inventory_snapshot_rows (
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    raw_id BIGINT NOT NULL REFERENCES amazon_fba_inventory_raw(id),
    inventory_key TEXT NOT NULL,
    row_action TEXT NOT NULL CHECK (
        row_action IN ('inserted', 'updated', 'skipped')
    ),
    PRIMARY KEY (sync_attempt_id, inventory_key)
);

CREATE TABLE IF NOT EXISTS amazon_fba_inventory_rejects (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, payload_checksum, error_code)
);

CREATE TABLE IF NOT EXISTS amazon_fba_inventory (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    inventory_key TEXT NOT NULL,
    asin TEXT NOT NULL,
    seller_sku TEXT NOT NULL,
    fn_sku TEXT,
    item_condition TEXT,
    product_name TEXT,
    total_quantity BIGINT NOT NULL,
    fulfillable_quantity BIGINT NOT NULL,
    inbound_working_quantity BIGINT NOT NULL,
    inbound_shipped_quantity BIGINT NOT NULL,
    inbound_receiving_quantity BIGINT NOT NULL,
    reserved_quantity BIGINT NOT NULL,
    researching_quantity BIGINT NOT NULL,
    unfulfillable_quantity BIGINT NOT NULL,
    source_updated_at TIMESTAMPTZ,
    source_raw_id BIGINT NOT NULL REFERENCES amazon_fba_inventory_raw(id),
    payload_checksum TEXT NOT NULL,
    last_seen_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    last_seen_at TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, inventory_key)
);

CREATE INDEX IF NOT EXISTS idx_amazon_fba_inventory_scope_active
    ON amazon_fba_inventory(store_id, marketplace, active, fulfillable_quantity);

CREATE TABLE IF NOT EXISTS amazon_ads_reports (
    sync_attempt_id UUID PRIMARY KEY REFERENCES amazon_sync_attempts(id),
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    reporting_api_version TEXT NOT NULL,
    report_id TEXT,
    report_type_id TEXT NOT NULL,
    ad_product TEXT NOT NULL,
    group_by TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    attribution_window_days INTEGER NOT NULL,
    request_body JSONB NOT NULL,
    report_status TEXT NOT NULL,
    report_created_at TIMESTAMPTZ,
    report_completed_at TIMESTAMPTZ,
    downloaded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, report_id)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_reports_scope_window
    ON amazon_ads_reports(
        store_id, marketplace, report_type_id, start_date, end_date, created_at DESC
    );

CREATE TABLE IF NOT EXISTS amazon_ads_campaign_raw (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    report_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    report_date DATE NOT NULL,
    campaign_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, row_key)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_campaign_raw_scope_date
    ON amazon_ads_campaign_raw(report_date, campaign_id);

CREATE TABLE IF NOT EXISTS amazon_ads_campaign_rejects (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, payload_checksum, error_code)
);

CREATE TABLE IF NOT EXISTS amazon_ads_campaign_daily (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    report_date DATE NOT NULL,
    campaign_id TEXT NOT NULL,
    campaign_name TEXT,
    campaign_status TEXT,
    campaign_budget_amount NUMERIC(20, 4),
    campaign_budget_type TEXT,
    campaign_bidding_strategy TEXT,
    impressions BIGINT NOT NULL,
    clicks BIGINT NOT NULL,
    spend NUMERIC(20, 4) NOT NULL,
    sales_1d NUMERIC(20, 4) NOT NULL,
    sales_7d NUMERIC(20, 4) NOT NULL,
    sales_14d NUMERIC(20, 4) NOT NULL,
    purchases_1d BIGINT NOT NULL,
    purchases_7d BIGINT NOT NULL,
    purchases_14d BIGINT NOT NULL,
    units_sold_clicks_1d BIGINT NOT NULL,
    units_sold_clicks_7d BIGINT NOT NULL,
    units_sold_clicks_14d BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    attribution_window_days INTEGER NOT NULL,
    provisional_until DATE NOT NULL,
    report_completed_at TIMESTAMPTZ NOT NULL,
    source_raw_id BIGINT NOT NULL REFERENCES amazon_ads_campaign_raw(id),
    payload_checksum TEXT NOT NULL,
    last_seen_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, report_date, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_campaign_daily_scope_date
    ON amazon_ads_campaign_daily(
        store_id, marketplace, report_date DESC, active
    );

CREATE TABLE IF NOT EXISTS amazon_ads_search_term_raw (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    report_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    report_date DATE NOT NULL,
    campaign_id TEXT NOT NULL,
    ad_group_id TEXT NOT NULL,
    search_term TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, row_key)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_search_term_raw_scope_date
    ON amazon_ads_search_term_raw(report_date, campaign_id, ad_group_id);

CREATE TABLE IF NOT EXISTS amazon_ads_search_term_rejects (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, payload_checksum, error_code)
);

CREATE TABLE IF NOT EXISTS amazon_ads_search_term_daily (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    report_date DATE NOT NULL,
    row_key TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    campaign_name TEXT,
    ad_group_id TEXT NOT NULL,
    ad_group_name TEXT,
    keyword_id TEXT,
    keyword TEXT,
    match_type TEXT,
    search_term TEXT NOT NULL,
    impressions BIGINT NOT NULL,
    clicks BIGINT NOT NULL,
    spend NUMERIC(20, 4) NOT NULL,
    sales_1d NUMERIC(20, 4) NOT NULL,
    sales_7d NUMERIC(20, 4) NOT NULL,
    sales_14d NUMERIC(20, 4) NOT NULL,
    purchases_1d BIGINT NOT NULL,
    purchases_7d BIGINT NOT NULL,
    purchases_14d BIGINT NOT NULL,
    units_sold_clicks_1d BIGINT NOT NULL,
    units_sold_clicks_7d BIGINT NOT NULL,
    units_sold_clicks_14d BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    attribution_window_days INTEGER NOT NULL,
    provisional_until DATE NOT NULL,
    report_completed_at TIMESTAMPTZ NOT NULL,
    source_raw_id BIGINT NOT NULL REFERENCES amazon_ads_search_term_raw(id),
    payload_checksum TEXT NOT NULL,
    last_seen_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, report_date, row_key)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_search_term_daily_scope_date
    ON amazon_ads_search_term_daily(
        store_id, marketplace, report_date DESC, active
    );

CREATE INDEX IF NOT EXISTS idx_amazon_ads_search_term_daily_term
    ON amazon_ads_search_term_daily(
        store_id, marketplace, search_term, report_date DESC
    );

CREATE TABLE IF NOT EXISTS amazon_ads_purchased_product_raw (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    report_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    report_date DATE NOT NULL,
    campaign_id TEXT NOT NULL,
    ad_group_id TEXT NOT NULL,
    advertised_asin TEXT NOT NULL,
    purchased_asin TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, row_key)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_purchased_product_raw_scope_date
    ON amazon_ads_purchased_product_raw(
        report_date, campaign_id, advertised_asin, purchased_asin
    );

CREATE TABLE IF NOT EXISTS amazon_ads_purchased_product_rejects (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sync_attempt_id, payload_checksum, error_code)
);

CREATE TABLE IF NOT EXISTS amazon_ads_purchased_product_daily (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    report_date DATE NOT NULL,
    row_key TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    campaign_name TEXT,
    ad_group_id TEXT NOT NULL,
    ad_group_name TEXT,
    keyword_id TEXT,
    match_type TEXT,
    portfolio_id TEXT,
    advertised_asin TEXT NOT NULL,
    advertised_sku TEXT,
    purchased_asin TEXT NOT NULL,
    sales_1d NUMERIC(20, 4) NOT NULL,
    sales_7d NUMERIC(20, 4) NOT NULL,
    sales_14d NUMERIC(20, 4) NOT NULL,
    sales_30d NUMERIC(20, 4) NOT NULL,
    purchases_1d BIGINT NOT NULL,
    purchases_7d BIGINT NOT NULL,
    purchases_14d BIGINT NOT NULL,
    purchases_30d BIGINT NOT NULL,
    units_sold_clicks_1d BIGINT NOT NULL,
    units_sold_clicks_7d BIGINT NOT NULL,
    units_sold_clicks_14d BIGINT NOT NULL,
    units_sold_clicks_30d BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    attribution_window_days INTEGER NOT NULL,
    provisional_until DATE NOT NULL,
    report_completed_at TIMESTAMPTZ NOT NULL,
    source_raw_id BIGINT NOT NULL REFERENCES amazon_ads_purchased_product_raw(id),
    payload_checksum TEXT NOT NULL,
    last_seen_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace, report_date, row_key)
);

CREATE INDEX IF NOT EXISTS idx_amazon_ads_purchased_product_daily_scope_date
    ON amazon_ads_purchased_product_daily(
        store_id, marketplace, report_date DESC, active
    );

CREATE INDEX IF NOT EXISTS idx_amazon_ads_purchased_product_daily_asin
    ON amazon_ads_purchased_product_daily(
        store_id, marketplace, purchased_asin, report_date DESC
    );

CREATE TABLE IF NOT EXISTS amazon_settlement_reports (
    report_id TEXT PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace_scope TEXT NOT NULL,
    report_type TEXT NOT NULL,
    report_document_id TEXT,
    processing_status TEXT NOT NULL,
    data_start_time TIMESTAMPTZ,
    data_end_time TIMESTAMPTZ,
    report_created_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ,
    report_checksum TEXT,
    settlement_id TEXT,
    source_row_count BIGINT,
    normalized_row_count BIGINT,
    rejected_row_count BIGINT,
    reconciliation_delta NUMERIC(20, 4),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_amazon_settlement_reports_scope_created
    ON amazon_settlement_reports(
        store_id, marketplace_scope, report_created_at DESC
    );

CREATE TABLE IF NOT EXISTS amazon_settlement_raw (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    report_id TEXT NOT NULL REFERENCES amazon_settlement_reports(report_id),
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace_scope TEXT NOT NULL,
    settlement_id TEXT,
    line_no INTEGER NOT NULL,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (report_id, line_no, payload_checksum)
);

CREATE INDEX IF NOT EXISTS idx_amazon_settlement_raw_scope_settlement
    ON amazon_settlement_raw(store_id, marketplace_scope, settlement_id, line_no);

CREATE TABLE IF NOT EXISTS amazon_settlement_rejects (
    id BIGSERIAL PRIMARY KEY,
    sync_attempt_id UUID NOT NULL REFERENCES amazon_sync_attempts(id),
    report_id TEXT NOT NULL REFERENCES amazon_settlement_reports(report_id),
    line_no INTEGER,
    payload JSONB NOT NULL,
    payload_checksum TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (report_id, line_no, payload_checksum, error_code)
);

CREATE TABLE IF NOT EXISTS amazon_settlement_lines (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace_scope TEXT NOT NULL,
    settlement_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    settlement_start_time TIMESTAMPTZ,
    settlement_end_time TIMESTAMPTZ,
    deposit_time TIMESTAMPTZ,
    total_amount NUMERIC(20, 4),
    currency CHAR(3) NOT NULL,
    transaction_type TEXT,
    order_id TEXT,
    merchant_order_id TEXT,
    adjustment_id TEXT,
    shipment_id TEXT,
    marketplace_name TEXT,
    amount_type TEXT,
    amount_description TEXT,
    amount NUMERIC(20, 4),
    fulfillment_id TEXT,
    posted_time TIMESTAMPTZ,
    order_item_code TEXT,
    merchant_order_item_id TEXT,
    merchant_adjustment_item_id TEXT,
    sku TEXT,
    quantity_purchased BIGINT,
    promotion_id TEXT,
    report_id TEXT NOT NULL REFERENCES amazon_settlement_reports(report_id),
    report_created_at TIMESTAMPTZ NOT NULL,
    source_raw_id BIGINT NOT NULL REFERENCES amazon_settlement_raw(id),
    payload_checksum TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        source, store_id, marketplace_scope, settlement_id, line_no
    )
);

CREATE INDEX IF NOT EXISTS idx_amazon_settlement_lines_period
    ON amazon_settlement_lines(
        store_id, marketplace_scope, settlement_end_time DESC, active
    );

CREATE INDEX IF NOT EXISTS idx_amazon_settlement_lines_order
    ON amazon_settlement_lines(store_id, order_id)
    WHERE order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS amazon_settlement_periods (
    source TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace_scope TEXT NOT NULL,
    settlement_id TEXT NOT NULL,
    settlement_start_time TIMESTAMPTZ,
    settlement_end_time TIMESTAMPTZ,
    deposit_time TIMESTAMPTZ,
    currency CHAR(3) NOT NULL,
    net_payout NUMERIC(20, 4) NOT NULL,
    detail_amount_total NUMERIC(20, 4) NOT NULL,
    reconciliation_delta NUMERIC(20, 4) NOT NULL,
    summary_row_count BIGINT NOT NULL,
    detail_row_count BIGINT NOT NULL,
    marketplace_name_count BIGINT NOT NULL,
    report_id TEXT NOT NULL REFERENCES amazon_settlement_reports(report_id),
    report_created_at TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, store_id, marketplace_scope, settlement_id)
);

CREATE INDEX IF NOT EXISTS idx_amazon_settlement_periods_scope_end
    ON amazon_settlement_periods(
        store_id, marketplace_scope, settlement_end_time DESC, active
    );
