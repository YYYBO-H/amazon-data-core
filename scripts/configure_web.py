#!/usr/bin/env python3
"""Collect Amazon credentials in a private browser page served on localhost.

The page never sends credentials to an Agent or a remote service. It uses only
the Python standard library so it is available before the Core image is built.
"""

from __future__ import annotations

import argparse
import html
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import configure


MAX_FORM_BYTES = 64 * 1024


def _first(form: dict[str, list[str]], key: str, default: str = "") -> str:
    return form.get(key, [default])[0].strip()


def _marketplace_code(existing: dict[str, str]) -> str:
    current = existing.get("AMAZON_MARKETPLACE_ID", "")
    for code, (marketplace, *_rest) in configure.MARKETPLACES.items():
        if marketplace == current:
            return code
    return "CUSTOM" if current else "US"


def build_configuration(
    form: dict[str, list[str]], existing: dict[str, str]
) -> dict[str, str]:
    """Validate a browser form without ever returning secrets to the browser."""
    preset = _first(form, "marketplace", _marketplace_code(existing)).upper()
    if preset == "CUSTOM":
        marketplace = _first(form, "marketplace_id", existing.get("AMAZON_MARKETPLACE_ID", ""))
        if not marketplace:
            raise ValueError("Marketplace ID is required for CUSTOM")
        region = _first(form, "region", existing.get("AMAZON_REGION", "NA")).upper()
        timezone = _first(form, "timezone", existing.get("AMAZON_TIMEZONE", "UTC"))
        currency = _first(form, "currency", existing.get("AMAZON_CURRENCY", "USD")).upper()
        ads_region_default = existing.get("AMAZON_AD_REGION", region) or region
    elif preset in configure.MARKETPLACES:
        marketplace, region, timezone, currency, ads_region_default = configure.MARKETPLACES[preset]
    else:
        raise ValueError("choose a supported marketplace or CUSTOM")

    store_default = existing.get("AMAZON_STORE_ID", f"amazon-{preset.lower()}")
    values = dict(existing)
    values.update(
        {
            "DATABASE_URL": existing.get(
                "DATABASE_URL", "postgresql://data_core:data_core@localhost:55432/data_core"
            ),
            "LOAD_DEMO": "false",
            "AMAZON_STORE_ID": _first(form, "store_id", store_default),
            "AMAZON_MARKETPLACE_ID": marketplace,
            "AMAZON_REGION": region,
            "AMAZON_TIMEZONE": timezone,
            "AMAZON_CURRENCY": currency,
            "AMAZON_AD_REGION": ads_region_default,
        }
    )

    for field in ("AMAZON_CLIENT_ID", "AMAZON_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN"):
        submitted = _first(form, field.lower())
        values[field] = submitted or existing.get(field, "")
        if not values[field]:
            raise ValueError(f"{field} is required")

    if _first(form, "configure_ads") == "yes":
        for field in (
            "AMAZON_AD_CLIENT_ID",
            "AMAZON_AD_CLIENT_SECRET",
            "AMAZON_AD_REFRESH_TOKEN",
            "AMAZON_AD_PROFILE_ID",
        ):
            submitted = _first(form, field.lower())
            values[field] = submitted or existing.get(field, "")
            if not values[field]:
                raise ValueError(f"{field} is required when Amazon Ads is enabled")
        values["AMAZON_AD_REGION"] = _first(form, "amazon_ad_region", ads_region_default).upper()
    else:
        for field in (
            "AMAZON_AD_CLIENT_ID",
            "AMAZON_AD_CLIENT_SECRET",
            "AMAZON_AD_REFRESH_TOKEN",
            "AMAZON_AD_PROFILE_ID",
        ):
            values[field] = ""

    configure._validate(values)
    return values


def _secret_label(
    label: str,
    key: str,
    existing: dict[str, str],
    *,
    browser_required: bool = True,
) -> str:
    saved = bool(existing.get(key, ""))
    note = "<small>已保存；留空则保留原值</small>" if saved else "<small>仅写入本机 .env</small>"
    required = " required" if browser_required and not saved else ""
    return (
        f'<label>{html.escape(label)}{note}'
        f'<input type="password" name="{key.lower()}" autocomplete="off"{required}></label>'
    )


def render_form(existing: dict[str, str], token: str, error: str = "") -> str:
    preset = _marketplace_code(existing)
    options = "".join(
        f'<option value="{code}"{" selected" if code == preset else ""}>{code}</option>'
        for code in (*configure.MARKETPLACES.keys(), "CUSTOM")
    )
    ads_saved = configure.configuration_status(existing)["ads_configured"]
    error_box = f'<div class="error">{html.escape(error)}</div>' if error else ""
    store = html.escape(existing.get("AMAZON_STORE_ID", f"amazon-{preset.lower()}"), quote=True)
    marketplace_id = html.escape(existing.get("AMAZON_MARKETPLACE_ID", ""), quote=True)
    region = html.escape(existing.get("AMAZON_REGION", "NA"), quote=True)
    timezone = html.escape(existing.get("AMAZON_TIMEZONE", "UTC"), quote=True)
    currency = html.escape(existing.get("AMAZON_CURRENCY", "USD"), quote=True)
    ads_region = html.escape(existing.get("AMAZON_AD_REGION", "NA"), quote=True)
    checked = " checked" if ads_saved else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amazon Data Core 本地授权</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#172033;font:15px system-ui,-apple-system,sans-serif}}
main{{max-width:780px;margin:36px auto;padding:0 20px}}.card{{overflow:hidden;background:#fff;border:1px solid #dde2ea;border-radius:20px;box-shadow:0 18px 50px #19243a14}}
.header{{padding:30px 30px 22px;background:linear-gradient(135deg,#fff8eb,#fff)}}.eyebrow{{color:#9a5800;font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}}
h1{{margin:7px 0 8px;font-size:28px}}.lead{{color:#586174;margin:0}}.safe{{background:#eefaf2;color:#17663a;padding:12px 14px;border-radius:10px;margin:18px 0 0}}
.progress{{height:4px;background:#edf0f4}}.progress span{{display:block;width:33.333%;height:100%;background:#ff9900;transition:width .3s ease}}
.steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:20px 30px 0}}.step{{color:#8991a1;font-size:13px;font-weight:700}}.step b{{display:inline-grid;place-items:center;width:24px;height:24px;margin-right:6px;border-radius:50%;background:#edf0f4}}
.step.active{{color:#172033}}.step.active b{{background:#ff9900;color:#251500}}form{{padding:8px 30px 28px}}.panel{{animation:arrive .25s ease}}.panel[hidden]{{display:none}}@keyframes arrive{{from{{opacity:0;transform:translateX(10px)}}to{{opacity:1;transform:none}}}}
.error{{background:#fff0f0;color:#a82020;padding:12px 14px;border-radius:10px;margin:18px 0}}fieldset{{border:0;padding:0;margin:22px 0}}legend{{font-weight:750;font-size:20px;margin-bottom:6px}}.hint{{color:#687286;margin:0 0 18px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}label{{display:flex;flex-direction:column;gap:6px;font-weight:650}}small{{display:block;color:#768095;font-weight:400}}
input,select{{width:100%;border:1px solid #cfd6e2;border-radius:10px;padding:11px 12px;font:inherit;background:#fff}}input:focus,select:focus{{outline:3px solid #ffb15a66;border-color:#ff8a00}}
.check{{flex-direction:row;align-items:center;font-weight:650}}.check input{{width:auto}}.help{{padding:15px;border:1px solid #f0d5a7;background:#fffaf1;border-radius:12px;margin:16px 0}}.help p{{margin:0 0 8px}}a{{color:#9a5800;font-weight:700}}
.actions{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:24px}}.actions-right{{margin-left:auto}}button{{border:0;border-radius:10px;padding:12px 18px;font:inherit;font-weight:750;cursor:pointer}}button:focus-visible{{outline:3px solid #ffb15a88}}.primary{{background:#ff9900;color:#18120a}}.secondary,.cancel{{background:#eef0f4;color:#4b5567}}button[disabled]{{opacity:.65;cursor:wait}}
.ads-fields{{margin-top:16px}}.ads-fields[hidden],.custom-fields[hidden]{{display:none}}.foot{{font-size:13px;color:#768095;margin:18px 30px 26px}}@media(max-width:620px){{.grid{{grid-template-columns:1fr}}main{{margin:14px auto}}.header,form{{padding-left:20px;padding-right:20px}}.steps{{padding-left:20px;padding-right:20px}}.step{{font-size:0}}.step b{{font-size:13px}}}}@media(prefers-reduced-motion:reduce){{*{{animation:none!important;scroll-behavior:auto!important;transition:none!important}}}}
</style></head><body><main><section class="card">
<header class="header"><div class="eyebrow">仅在你的电脑上运行</div><h1>连接你的 Amazon 店铺</h1>
<p class="lead">分三步确认范围、SP-API 和可选广告授权。</p>
<div class="safe">🔒 凭证只写入本机权限为 0600 的 .env，不会进入 Agent 聊天记录。</div></header>
<div class="progress" aria-hidden="true"><span id="progress"></span></div>
<nav class="steps" aria-label="配置进度"><div class="step active"><b>1</b>店铺范围</div><div class="step"><b>2</b>SP-API</div><div class="step"><b>3</b>广告与确认</div></nav>
<form id="setup" method="post" action="/{token}/">
<input type="hidden" name="csrf" value="{token}">{error_box}
<section class="panel" data-panel="0"><fieldset><legend>先确认店铺范围</legend><p class="hint">选择站点后，区域、时区和货币会自动使用项目内置映射。</p><div class="grid">
<label>Marketplace<select id="marketplace" name="marketplace">{options}</select></label>
<label>本地店铺名称<input name="store_id" value="{store}" required></label>
</div><div id="custom-fields" class="grid custom-fields">
<label>自定义 Marketplace ID<input name="marketplace_id" value="{marketplace_id}"></label>
<label>区域（NA/EU/FE）<input name="region" value="{region}"></label>
<label>时区<input name="timezone" value="{timezone}"></label>
<label>货币<input name="currency" value="{currency}"></label>
</div></fieldset><div class="actions"><span></span><button class="primary" type="button" data-next>下一步：SP-API</button></div></section>
<section class="panel" data-panel="1" hidden><fieldset><legend>填写 SP-API 凭证</legend><p class="hint">三个值来自你自己组织的 private SP-API 应用与自授权。</p>
<div class="help"><p><strong>还没有这些值？</strong>先不要随便填写。让 Agent 按官方流程逐步带你完成。</p>
<a href="https://developer-docs.amazon.com/sp-api/docs/onboarding-overview" target="_blank" rel="noreferrer">打开 Amazon 官方自授权说明 ↗</a></div><div class="grid">
{_secret_label("LWA Client ID", "AMAZON_CLIENT_ID", existing)}
{_secret_label("LWA Client Secret", "AMAZON_CLIENT_SECRET", existing)}
{_secret_label("Self-authorization Refresh Token", "AMAZON_REFRESH_TOKEN", existing)}
</div></fieldset><div class="actions"><button class="secondary" type="button" data-back>上一步</button><button class="primary" type="button" data-next>下一步：可选广告</button></div></section>
<section class="panel" data-panel="2" hidden><fieldset><legend>广告授权与最终确认</legend><p class="hint">Amazon Ads 是独立授权。没有 Ads API 权限也可以先同步订单、FBA 库存和结算。</p>
<label class="check"><input id="configure-ads" type="checkbox" name="configure_ads" value="yes"{checked}> 同时配置 Amazon Ads</label>
<div id="ads-fields" class="grid ads-fields">
{_secret_label("Ads Client ID", "AMAZON_AD_CLIENT_ID", existing, browser_required=False)}
{_secret_label("Ads Client Secret", "AMAZON_AD_CLIENT_SECRET", existing, browser_required=False)}
{_secret_label("Ads Refresh Token", "AMAZON_AD_REFRESH_TOKEN", existing, browser_required=False)}
{_secret_label("Ads Profile ID", "AMAZON_AD_PROFILE_ID", existing, browser_required=False)}
<label>Ads 区域（NA/EU/FE）<input name="amazon_ad_region" value="{ads_region}"></label>
</div></fieldset><div class="actions"><button class="secondary" type="button" data-back>上一步</button><div class="actions-right"><button class="primary" id="submit" type="submit">保存并开始首次同步</button></div></div></section>
<div class="actions"><button class="cancel" type="submit" name="action" value="cancel" formnovalidate>取消配置</button></div>
</form><p class="foot">Amazon Data Core 不会创建 Amazon 应用，也不能绕过 Amazon 授权或角色审批。</p>
</section></main><script nonce="{token}">
const panels=[...document.querySelectorAll('.panel')], steps=[...document.querySelectorAll('.step')]; let current=0;
function show(index){{current=index;panels.forEach((p,i)=>p.hidden=i!==index);steps.forEach((s,i)=>s.classList.toggle('active',i<=index));document.querySelector('#progress').style.width=((index+1)/panels.length*100)+'%';window.scrollTo({{top:0,behavior:'smooth'}})}}
function validPanel(){{for(const field of panels[current].querySelectorAll('input,select')){{if(!field.checkValidity()){{field.reportValidity();return false}}}}return true}}
document.querySelectorAll('[data-next]').forEach(button=>button.addEventListener('click',()=>{{if(validPanel())show(current+1)}}));
document.querySelectorAll('[data-back]').forEach(button=>button.addEventListener('click',()=>show(current-1)));
document.querySelector('#setup').addEventListener('keydown',event=>{{if(event.key==='Enter'&&current<panels.length-1){{event.preventDefault();if(validPanel())show(current+1)}}}});
const marketplace=document.querySelector('#marketplace'), custom=document.querySelector('#custom-fields');function toggleCustom(){{custom.hidden=marketplace.value!=='CUSTOM'}}marketplace.addEventListener('change',toggleCustom);toggleCustom();
const ads=document.querySelector('#configure-ads'),adsFields=document.querySelector('#ads-fields');function toggleAds(){{adsFields.hidden=!ads.checked}}ads.addEventListener('change',toggleAds);toggleAds();
document.querySelector('#setup').addEventListener('submit',event=>{{if(event.submitter?.value==='cancel')return;const button=document.querySelector('#submit');button.disabled=true;button.textContent='正在安全保存…'}});
</script></body></html>"""


def render_success() -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>授权配置已保存</title>
<style>body{font:16px system-ui;background:#f6f7f9;color:#172033;margin:0}main{max-width:620px;margin:80px auto;background:#fff;border:1px solid #dde2ea;border-radius:18px;padding:36px;text-align:center;box-shadow:0 18px 50px #19243a14}h1{color:#147a42}.flow{display:flex;align-items:center;justify-content:center;gap:9px;margin:28px 0}.node{padding:9px 12px;border-radius:20px;background:#eefaf2;color:#17663a;font-size:13px;font-weight:700}.working{background:#fff4df;color:#965800;animation:pulse 1.3s infinite}.arrow{color:#a3aab7}@keyframes pulse{50%{opacity:.55}}</style>
</head><body><main><h1>✓ 本地授权配置已保存</h1><div class="flow"><span class="node">已保存</span><span class="arrow">→</span><span class="node working">验证授权</span><span class="arrow">→</span><span class="node working">首次同步</span></div><p>可以关闭此页面。Agent 会继续读取不含凭证的进度状态，并告诉你每个数据集的结果。</p></main></body></html>"""


class ConfigurationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], env_file: Path):
        self.env_file = env_file
        self.existing = configure.read_env(env_file)
        self.token = secrets.token_urlsafe(32)
        self.completed = threading.Event()
        self.cancelled = False
        super().__init__(address, ConfigurationHandler)


class ConfigurationHandler(BaseHTTPRequestHandler):
    server: ConfigurationServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _headers(self, status: HTTPStatus, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{self.server.token}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.end_headers()

    def _write_html(self, status: HTTPStatus, body: str) -> None:
        payload = body.encode("utf-8")
        self._headers(status)
        self.wfile.write(payload)

    def _valid_request(self) -> bool:
        path = urlsplit(self.path).path
        if path != f"/{self.server.token}/":
            self.send_error(HTTPStatus.NOT_FOUND, "Local authorization link is invalid or expired")
            return False
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host:
            self.send_error(HTTPStatus.FORBIDDEN, "Local authorization host did not match")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_request():
            return
        self._write_html(HTTPStatus.OK, render_form(self.server.existing, self.server.token))

    def do_POST(self) -> None:  # noqa: N802
        if not self._valid_request():
            return
        origin = self.headers.get("Origin")
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        # Chrome translation and some privacy extensions submit a local form
        # from an opaque origin. The unguessable URL/CSRF token plus strict Host
        # validation still prevent a remote page from forging this request.
        if origin not in (None, "null", expected_origin):
            self.send_error(HTTPStatus.FORBIDDEN, "Browser origin was not the local authorization page")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_FORM_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        if not secrets.compare_digest(_first(form, "csrf"), self.server.token):
            self.send_error(HTTPStatus.FORBIDDEN, "Local authorization form expired; reopen the latest link")
            return
        if _first(form, "action") == "cancel":
            self.server.cancelled = True
            self.server.completed.set()
            self._write_html(HTTPStatus.OK, "<h1>配置已取消，可以关闭页面。</h1>")
            return
        try:
            values = build_configuration(form, self.server.existing)
            configure.write_env(self.server.env_file, values)
        except ValueError as exc:
            self._write_html(
                HTTPStatus.BAD_REQUEST,
                render_form(self.server.existing, self.server.token, str(exc)),
            )
            return
        self.server.completed.set()
        self._write_html(HTTPStatus.OK, render_success())


def run_server(
    env_file: Path,
    host: str,
    port: int,
    *,
    open_browser: bool,
    timeout: int,
    ready_callback: Callable[[str], None] | None = None,
) -> int:
    server = ConfigurationServer((host, port), env_file)
    server.timeout = 1
    url = f"http://127.0.0.1:{server.server_port}/{server.token}/"
    print("Amazon credentials must not be sent through Agent chat.", flush=True)
    print(f"Private local authorization page: {url}", flush=True)
    if ready_callback is not None:
        ready_callback(url)
    if open_browser and not webbrowser.open(url):
        print("The browser did not open automatically. Open the private local URL above.", flush=True)

    deadline = time.monotonic() + timeout
    try:
        while not server.completed.is_set() and time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nLocal authorization cancelled; no credentials were changed.")
        return 130
    finally:
        server.server_close()

    if not server.completed.is_set():
        print("Local authorization timed out; no credentials were changed.")
        return 2
    if server.cancelled:
        print("Local authorization cancelled; no credentials were changed.")
        return 130
    print(f"Local authorization saved to {env_file} with permissions 0600.")
    print("No credential value was printed or sent to the Agent.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=configure.DEFAULT_ENV_PATH)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    return run_server(
        args.env_file,
        args.host,
        args.port,
        open_browser=not args.no_open,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
