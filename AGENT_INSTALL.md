# Agent installation contract

This file is an execution contract for an Agent operating on the user's local
computer. **Clone and run the existing repository. Do not generate a new app,
frontend, dashboard or replacement implementation from these documents.**

The goal is a verified local data service, not merely a cloned repository. An
Agent may run installation and verification commands, but the user must enter
Amazon secrets in the temporary localhost authorization page. If the Agent
cannot execute local shell commands, it must explain that limitation instead of
claiming the Core was installed.

## Full new-user flow

First run the secret-free machine preflight and parse its JSON:

```bash
python3 scripts/preflight.py
```

Follow the returned `next_action`. Do not continue past a failed prerequisite
or translate failure into a generic “try again” message. The current execution
matrix is:

- macOS: supported through Docker Desktop and the POSIX scripts;
- Linux: supported through Docker Engine/Compose v2 and the POSIX scripts;
- Windows with WSL2: use the Linux flow inside WSL2;
- native Windows PowerShell: not yet verified; preflight returns `use_wsl2`.

Before requesting credentials, establish which state the user is in:

1. no Professional selling account: explain that the private-app path is not
   available yet;
2. account but no registered private SP-API application: guide registration;
3. application exists but is not self-authorized: guide self-authorization;
4. LWA client ID, client secret and refresh token are ready: continue to the
   local configuration page;
5. local configuration exists: install, verify and synchronize without asking
   the user to paste saved values again.

For states 1–3, use
[`docs/amazon-authorization.md`](docs/amazon-authorization.md), show one step at
a time, and link to the relevant official Amazon page. Do not invent button
labels or use undated third-party screenshots as authoritative instructions.

From the repository root, show the user that this command will build local
Docker services, write a local `.env`, verify Amazon authorization and begin the
first synchronization. Then run:

```bash
./scripts/onboard.sh
```

The script first runs the credential-free installer. Only after Docker, the
database, Core and the MCP contract pass does it launch an independent
background worker and open a temporary Amazon configuration page on
`127.0.0.1`. The launcher returns after the page is ready; the authorization
server and subsequent first sync do not depend on the Agent shell remaining
open. When the page opens:

1. tell the user to fill and submit the local page personally;
2. do not ask the user to send credentials through chat;
3. do not fill the credential fields, read browser form contents, or automate
   the page;
4. do not echo, inspect, print, summarize or copy `.env`;
5. tell the user that submission starts first synchronization automatically;
6. when asked for progress, run
   `python3 scripts/onboard_background.py status` and report its status. Once
   the Core is running, use `http://localhost:8080/v1/sync-status` for
   credential-free per-dataset pages, row counts, errors and recovery commands.

If the default browser does not open, direct the user to the exact private
`http://127.0.0.1:...` URL printed by the command. That URL is temporary and
must not be published or sent to another machine. The server binds only to
localhost and shuts down after submission, cancellation or timeout.
Runtime state and a credential-free diagnostic log are stored under the
Git-ignored `.amazon-data-core/` directory with owner-only permissions.

If the user has no private SP-API application or refresh token yet, pause the
authorization portion and open [`docs/amazon-authorization.md`](docs/amazon-authorization.md).
Do not imply that cloning this repository creates or approves an Amazon app.

The onboarding result is successful only when the background status is
`complete`; this is written only after synchronization outputs
`First sync passed`. If status is `sync_failed`, inspect the credential-free
`.amazon-data-core/onboard.log` and state the exact failed dataset. Do not
describe successful LWA token exchange as proof that every required Amazon role
is present.

Do not open a visual app builder or reinterpret `docs/project-scope.md` as a
request to build a user interface. The running Core already exposes its local
status page, HTTP API and MCP server.

## Install without an Amazon account

For an empty local Core and MCP contract verification only:

```bash
./scripts/install.sh
```

Installation checks:

```bash
curl --fail http://localhost:8080/health
docker compose exec -T core amazon-data-core doctor
docker compose exec -T core python scripts/verify_mcp.py
docker compose exec -T core amazon-data-core status
```

The human-readable status page is `http://localhost:8080`; it polls the same
read-only `/v1/sync-status` response every three seconds. Do not infer progress
from elapsed time: report only the attempt state and counts returned by the
Core. `safe_to_analyze` becomes true only after every required dataset is ready
and the latest data-quality result is clear.

The installer defaults to an empty database. Demonstration data is opt-in with
`LOAD_DEMO=true docker compose up --build`.

## Rerun configuration or synchronization

```bash
python3 scripts/onboard_background.py launch
```

If a browser is genuinely unavailable and the Agent can hand over a real TTY to
the user, use `./scripts/onboard.sh --terminal-config` or
`python3 scripts/configure.py` as the fallback. Never pipe credentials into
either installer.

If configuration already exists and only the complete workflow needs to be
validated, use:

```bash
./scripts/onboard.sh --use-existing-config
```

The config status command returns only booleans and file permissions:

```bash
python3 scripts/configure.py --status
```

## MCP host configuration

Print a ready-to-copy generic stdio configuration with the correct absolute
repository path:

```bash
python3 scripts/configure.py --mcp-config
```

Its command is equivalent to:

```json
{
  "mcpServers": {
    "amazon-data-core": {
      "command": "docker",
      "args": ["compose", "exec", "-T", "core", "amazon-data-core", "mcp"],
      "cwd": "/absolute/path/to/amazon-data-core"
    }
  }
}
```

Different Agent products use different UI locations and config filenames; do
not claim that one config file is universal. The protocol exposed by the Core is
standard MCP stdio, and its nine tools are read-only.

## Dataset behavior

- Orders resume from the last committed page after a retryable failure.
- FBA inventory is a complete current snapshot; it excludes FBM/MFN inventory.
- Settlements require the Finance and Accounting role and only include reports
  already generated and closed by Amazon.
- Ads is optional and uses separate approved credentials and a Profile ID.
- Campaign, search-term and purchased-product Ads reports have different grains
  and must never be added together.
- Recent attributed Ads results are provisional, not final store revenue or
  profit.

One dataset failure does not roll back other successful datasets. The onboarding
script nevertheless exits nonzero and lists every failed step so an Agent cannot
mistake partial coverage for a complete installation.
