# Instructions for local coding agents

This repository is an existing local-first application. Do not replace it with
a newly generated frontend, dashboard, database or Agent project.

## If the user asks you to install or try the project

1. Read [`AGENT_INSTALL.md`](AGENT_INSTALL.md) completely.
2. From the repository root run `python3 scripts/preflight.py` and parse its JSON.
3. Follow `next_action`; do not guess past a failed prerequisite.
4. On macOS, Linux or WSL2, start the guided flow with `./scripts/onboard.sh`.
5. Native Windows PowerShell is not verified yet. If preflight returns
   `use_wsl2`, explain that limitation and continue inside WSL2.
6. Let the user enter Amazon secrets personally in the temporary localhost
   page. Never request secrets in chat, inspect form fields, or read `.env`.
7. Treat onboarding as complete only when
   `python3 scripts/onboard_background.py status` reports `complete`.

If the user has not created and self-authorized a private SP-API application,
guide them with [`docs/amazon-authorization.md`](docs/amazon-authorization.md)
before opening the local credentials page. Cloning this repository does not
create Amazon credentials.

## If the user asks you to analyze Amazon data

Read [`skills/amazon-data-core/SKILL.md`](skills/amazon-data-core/SKILL.md) and
follow its verification workflow. The nine MCP tools are read-only. Never
describe data as safe to use when the returned health or `safe_to_analyze`
state says otherwise.

## Development guardrails

- Preserve the local-first boundary: business data and credentials stay on the
  user's machine unless the user explicitly configures another destination.
- Keep the Core usable without a custom chat UI; Agent products connect over
  standard MCP stdio.
- Do not add automated advertising changes or other write actions.
- Update tests and user-facing documentation when behavior changes.
- Run `python3 -m pytest` in the project environment before claiming success.
