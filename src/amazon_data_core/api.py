from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .contracts import DatasetRunIn, RuleIn, ScopeIn
from .db import connect
from .engine import health_summary, ingest_run, register_scope, run_checks, upsert_rule

app = FastAPI(title="Amazon Data Core", version="0.4.1")


@app.get("/health")
def health() -> dict[str, str]:
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/v1/scopes", status_code=201)
def create_scope(scope: ScopeIn) -> dict[str, bool]:
    with connect() as conn:
        register_scope(conn, scope)
    return {"registered": True}


@app.post("/v1/rules", status_code=201)
def create_rule(rule: RuleIn) -> dict[str, bool]:
    with connect() as conn:
        upsert_rule(conn, rule)
    return {"saved": True}


@app.post("/v1/runs", status_code=201)
def create_run(run: DatasetRunIn) -> dict:
    with connect() as conn:
        return ingest_run(conn, run)


@app.post("/v1/checks/run")
def execute_checks() -> dict:
    with connect() as conn:
        return run_checks(conn)


@app.get("/v1/data-health")
def data_health() -> dict:
    with connect() as conn:
        return health_summary(conn)


@app.get("/v1/issues")
def issues() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT rule_code, source, store_id, marketplace, dataset,
                      target_date, check_status, severity, details, evaluated_at
               FROM v_open_issues ORDER BY evaluated_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/v1/datasets")
def datasets() -> list[dict]:
    """Current accepted data plus its complete source lineage."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT
                   c.source, c.store_id, c.marketplace, c.dataset,
                   c.business_date, c.fetched_at, c.source_updated_at,
                   c.version_at, c.ingestion_status, c.source_count,
                   c.normalized_count, c.duplicate_count, c.error_count, c.checksum,
                   r.timezone, r.currency, r.raw_reference, r.schema_version,
                   r.formula_version, r.is_provisional, r.correction_of_run_id,
                   r.id AS run_id
               FROM current_dataset_state c
               JOIN dataset_runs r ON r.id = c.run_id
               ORDER BY c.store_id, c.marketplace, c.dataset"""
        ).fetchall()
    return [dict(row) for row in rows]


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Amazon Data Core</title><style>
body{font:16px system-ui;background:#0f172a;color:#e2e8f0;margin:0;padding:40px}main{max-width:760px;margin:auto}
.card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:28px;margin-top:28px}.clear{color:#4ade80}.warning{color:#facc15}.critical{color:#fb7185}.unknown{color:#94a3b8}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-top:24px}.metric{background:#0f172a;border-radius:10px;padding:16px}.metric b{display:block;font-size:28px}small{color:#94a3b8}a{color:#7dd3fc}
</style></head><body><main><h1>Amazon Data Core</h1><p>可信的 Amazon 经营数据底座</p><section class="card"><h2 id="headline">正在读取…</h2><div class="grid" id="metrics"></div><p><small id="checked"></small></p></section><p><a href="/docs">打开 API 文档 →</a></p></main>
<script>fetch('/v1/data-health').then(r=>r.json()).then(d=>{let h=document.querySelector('#headline');h.textContent=d.headline;h.className=d.tone;document.querySelector('#metrics').innerHTML=['passed','failed','skipped','error','open_issues','recovered_historical'].map(k=>`<div class="metric"><b>${d[k]}</b><small>${k}</small></div>`).join('');document.querySelector('#checked').textContent=`最近检查 ${d.checked}/${d.expected_checks} · ${d.last_checked_at||'暂无时间'}`}).catch(()=>{document.querySelector('#headline').textContent='数据健康暂时读不到';document.querySelector('#headline').className='unknown'})</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE
