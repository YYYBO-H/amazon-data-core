from __future__ import annotations

import os
import secrets

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .contracts import DatasetRunIn, RuleIn, ScopeIn
from .db import connect
from .engine import health_summary, ingest_run, register_scope, run_checks, upsert_rule
from .sync_status import sync_overview_from_database

app = FastAPI(title="Amazon Data Core", version="0.4.2")


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


def _configured(*names: str) -> bool:
    return all(bool(os.getenv(name, "").strip()) for name in names)


@app.get("/v1/sync-status")
def sync_status() -> dict:
    """Credential-free progress for the local status page and Agent."""
    with connect() as conn:
        return sync_overview_from_database(
            conn,
            sp_api_configured=_configured(
                "AMAZON_CLIENT_ID",
                "AMAZON_CLIENT_SECRET",
                "AMAZON_REFRESH_TOKEN",
                "AMAZON_STORE_ID",
                "AMAZON_MARKETPLACE_ID",
            ),
            ads_configured=_configured(
                "AMAZON_AD_CLIENT_ID",
                "AMAZON_AD_CLIENT_SECRET",
                "AMAZON_AD_REFRESH_TOKEN",
                "AMAZON_AD_PROFILE_ID",
            ),
            demo_mode=os.getenv("LOAD_DEMO", "false").lower() == "true",
        )


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amazon Data Core · 同步状态</title><style nonce="__NONCE__">
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#172033;font:15px system-ui,-apple-system,sans-serif}main{max-width:980px;margin:36px auto;padding:0 20px}
.hero,.section{background:#fff;border:1px solid #dfe4eb;border-radius:20px;box-shadow:0 16px 45px #19243a10}.hero{overflow:hidden}.hero-main{padding:30px}.eyebrow{color:#9a5800;font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}h1{margin:7px 0 8px;font-size:30px}.lead{color:#657084;margin:0}.summary{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:22px}.badge{display:inline-flex;align-items:center;gap:7px;border-radius:20px;padding:8px 12px;background:#eef0f4;color:#586174;font-weight:750}.badge.syncing{background:#fff4df;color:#965800}.badge.complete{background:#e9f8ef;color:#17663a}.badge.attention{background:#fff0f0;color:#a82020}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}.syncing .dot{animation:pulse 1.2s infinite}.safe{font-weight:700;color:#17663a}.progress{height:5px;background:#edf0f4}.progress span{display:block;width:0;height:100%;background:#ff9900;transition:width .35s ease}
.section{padding:26px;margin-top:20px}.section-head{display:flex;align-items:end;justify-content:space-between;gap:14px;margin-bottom:16px}.section h2{margin:0;font-size:20px}.muted{color:#768095}.datasets{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.dataset{border:1px solid #e1e5ec;border-radius:14px;padding:17px;transition:border-color .2s,transform .2s}.dataset.syncing{border-color:#efb65e}.dataset.attention,.dataset.needs_setup{border-color:#efb3b3}.dataset.ready{border-color:#a7d7b9}.dataset:hover{transform:translateY(-1px)}.dataset-top{display:flex;align-items:center;justify-content:space-between;gap:10px}.dataset h3{margin:0;font-size:16px}.state{font-size:12px;font-weight:800;border-radius:12px;padding:5px 9px;background:#eef0f4;color:#657084}.ready .state{background:#e9f8ef;color:#17663a}.syncing .state{background:#fff4df;color:#965800}.attention .state,.needs_setup .state{background:#fff0f0;color:#a82020}.numbers{display:flex;gap:18px;margin-top:14px}.number b{display:block;font-size:20px}.number small,.detail{color:#768095}.detail{font-size:13px;margin:13px 0 0}.recovery{background:#f7f8fa;border-radius:10px;padding:11px;margin-top:12px;font-size:13px}.command{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px;font:12px ui-monospace,SFMono-Regular,monospace;color:#374151}.copy{border:0;border-radius:8px;background:#fff;color:#7a4900;padding:6px 9px;font:inherit;font-weight:800;cursor:pointer}.quality{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.metric{background:#f7f8fa;border-radius:12px;padding:14px}.metric b{display:block;font-size:23px}.metric small{color:#768095}.links{display:flex;gap:16px;flex-wrap:wrap;margin:22px 2px}a{color:#8c5200;font-weight:750;text-decoration:none}a:hover{text-decoration:underline}.error-box{background:#fff0f0;color:#a82020;border-radius:12px;padding:14px}.hidden{display:none}
@keyframes pulse{50%{opacity:.35}}@media(max-width:720px){main{margin:16px auto}.datasets{grid-template-columns:1fr}.quality{grid-template-columns:repeat(3,1fr)}.hero-main,.section{padding:20px}}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body><main>
<section class="hero"><div class="hero-main"><div class="eyebrow">Local · Read-only</div><h1 id="headline">正在读取本机同步状态…</h1><p class="lead" id="subline">页面只展示不含凭证的数据库进度，每 3 秒自动更新。</p><div class="summary"><span class="badge" id="overall"><i class="dot"></i><span>连接中</span></span><span class="safe" id="safe">尚未确认是否可分析</span><span class="muted" id="mode"></span></div></div><div class="progress" aria-label="同步进度"><span id="progress"></span></div></section>
<div class="error-box hidden" id="error">暂时无法读取状态；Core 恢复后页面会自动重试。</div>
<section class="section"><div class="section-head"><div><h2>数据接入</h2><span class="muted">真实分页数和处理行数来自本机 PostgreSQL</span></div><span class="muted" id="updated"></span></div><div class="datasets" id="datasets"></div></section>
<section class="section"><div class="section-head"><div><h2>数据质量</h2><span class="muted" id="quality-headline">等待检查</span></div><a href="/v1/issues">查看问题 JSON →</a></div><div class="quality" id="quality"></div></section>
<nav class="links"><a href="/docs">API 文档 →</a><a href="/v1/sync-status">同步状态 JSON →</a><a href="/v1/data-health">数据健康 JSON →</a></nav>
</main><script nonce="__NONCE__">
const labels={ready:'已完成',syncing:'同步中',attention:'需处理',waiting:'等待中',needs_setup:'待配置',optional:'可选未配置'};
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function datasetCard(item){const attempt=item.attempt||{},current=item.current||{};const pulled=Number(attempt.rows_pulled??current.source_count??0);const accepted=Number((attempt.rows_inserted??0)+(attempt.rows_updated??0))||Number(current.normalized_count??0);const when=attempt.updated_at||current.fetched_at;const detail=item.status==='syncing'?`已完成 ${attempt.pages_completed||0} 页`:item.status==='ready'?(when?`最近更新 ${new Date(when).toLocaleString()}`:'已有可用数据'):item.data_available?'上次数据仍可用，但最新同步失败':'尚未产生可用数据';const recovery=item.recovery?`<div class="recovery">${esc(item.recovery.message)}<div class="command"><code>${esc(item.recovery.command)}</code><button class="copy" data-command="${esc(item.recovery.command)}">复制</button></div></div>`:'';return `<article class="dataset ${esc(item.status)}"><div class="dataset-top"><h3>${esc(item.label)}${item.optional?' <small class="muted">· 可选</small>':''}</h3><span class="state">${labels[item.status]||esc(item.status)}</span></div><div class="numbers"><span class="number"><b>${pulled.toLocaleString()}</b><small>源记录</small></span><span class="number"><b>${accepted.toLocaleString()}</b><small>写入/更新</small></span><span class="number"><b>${Number(attempt.rows_errored??current.error_count??0).toLocaleString()}</b><small>错误</small></span></div><p class="detail">${esc(detail)}${attempt.error_type?` · ${esc(attempt.error_type)}`:''}</p>${recovery}</article>`}
function render(data){const overall=data.overall;document.querySelector('#headline').textContent=overall.headline;const badge=document.querySelector('#overall');badge.className='badge '+overall.status;badge.querySelector('span').textContent=overall.status==='complete'?'同步完成':overall.status==='syncing'?'正在同步':overall.status==='attention'?'需要处理':overall.status==='needs_setup'?'等待配置':'准备中';document.querySelector('#safe').textContent=overall.safe_to_analyze?'✓ 当前数据可以交给 Agent 分析':'当前数据尚未确认可安全分析';document.querySelector('#mode').textContent=overall.demo_mode?'演示数据模式':'';document.querySelector('#progress').style.width=overall.progress_percent+'%';document.querySelector('#datasets').innerHTML=data.datasets.map(datasetCard).join('');const q=data.quality;document.querySelector('#quality-headline').textContent=q.headline||'等待检查';const metrics=[['通过',q.passed],['失败',q.failed],['跳过',q.skipped],['错误',q.error],['待处理',q.open_issues],['已恢复',q.recovered_historical]];document.querySelector('#quality').innerHTML=metrics.map(([label,value])=>`<div class="metric"><b>${Number(value||0).toLocaleString()}</b><small>${label}</small></div>`).join('');document.querySelector('#updated').textContent=q.last_checked_at?'最近检查 '+new Date(q.last_checked_at).toLocaleString():'尚无检查时间';document.querySelector('#error').classList.add('hidden')}
async function load(){try{const response=await fetch('/v1/sync-status',{cache:'no-store'});if(!response.ok)throw new Error('status unavailable');render(await response.json())}catch(error){document.querySelector('#error').classList.remove('hidden')}}
document.addEventListener('click',async event=>{const button=event.target.closest('[data-command]');if(!button)return;await navigator.clipboard.writeText(button.dataset.command);button.textContent='已复制';setTimeout(()=>button.textContent='复制',1400)});load();setInterval(load,3000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(
        PAGE.replace("__NONCE__", nonce),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; connect-src 'self'; "
                f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
