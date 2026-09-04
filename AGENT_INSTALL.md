# Agent installation contract

This file is an execution contract for an Agent operating on the user's local
computer. **Clone and run the existing repository. Do not generate a new app,
frontend, dashboard or replacement implementation from these documents.**

The goal is a verified local data service, not merely a cloned repository. An
Agent may run installation and verification commands, but the user must enter
Amazon secrets directly into the terminal. If the Agent cannot execute local
shell commands, it must explain that limitation instead of claiming the Core was
installed.

## Full new-user flow

From the repository root, show the user that this command will build local
Docker services, write a local `.env`, verify Amazon authorization and begin the
first synchronization. Then run:

```bash
./scripts/onboard.sh
```

The script first runs the credential-free installer. Only after Docker, the
database, Core and the MCP contract pass does it start the interactive Amazon
configuration. When the prompts appear:

1. give control of the terminal to the user;
2. do not ask the user to send credentials through chat;
3. do not echo, inspect, print, summarize or copy `.env`;
4. resume after `Saved local configuration` appears.

If the user has no private SP-API application or refresh token yet, pause the
authorization portion and open [`docs/amazon-authorization.md`](docs/amazon-authorization.md).
Do not imply that cloning this repository creates or approves an Amazon app.

The onboarding result is successful only when the final output says
`First sync passed`. If it reports failures, state the exact failed dataset. Do
not describe successful LWA token exchange as proof that every required Amazon
role is present.

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

The installer defaults to an empty database. Demonstration data is opt-in with
`LOAD_DEMO=true docker compose up --build`.

## Rerun configuration or synchronization

```bash
python3 scripts/configure.py
./scripts/sync-all.sh
```

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
