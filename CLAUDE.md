
# CLAUDE.md — OpenWorker (downsized build)

> **Scope of this fork:** OpenWorker is being trimmed to a single, narrow use case.
> Only three things are in scope. Everything outside them is out of scope and should
> be removed or left un-extended.
>
> | Axis | In scope | Out of scope |
> |------|----------|--------------|
> | **Platform** | macOS on **Apple Silicon** only | Windows, Intel macs, Linux |
> | **Model keys** | **Together AI** (`TOGETHER_API_KEY`) | OpenAI/Anthropic/Gemini/Ollama/Fireworks keys |
> | **Connectors** | **Google Calendar** only | Slack, Gmail, GitHub, Jira, Notion, HubSpot, and all other integrations |

## Important architecture facts (read before editing)

- **Together AI is not its own provider.** It rides on the OpenAI-compatible client
  (`coworker/providers/openai_provider.py`) pointed at `https://api.together.xyz/v1`.
  The Together descriptor lives in `coworker/providers/registry.py` and its model list in
  `coworker/providers/matrix.py`. **Keep `openai_provider.py`** — it is the Together transport.
- **Google Calendar shares connector infrastructure** with the other ~24 integrations.
  Several shared files (`integration_tools.py`, `descriptors.py`, `tool_defs.py`) must be
  **pruned internally**, not deleted — keep the GCal parts, strip the rest.
- This file is a **manifest/plan only**. It does not itself delete anything. Execute the
  REMOVE list deliberately, then run the test suite.

---

## KEEP — files/dirs the use case depends on

### Core runtime (keep all)
- `coworker/` core modules: `agent.py`, `engine.py`, `cli.py`, `config.py`, `secrets.py`,
  `sessions.py`, `conversations.py`, `permissions.py`, `risk.py`, `audit.py`, `inbox*.py`,
  `interactions.py`, `attachments.py`, `pdf_support.py`, `catalog.py`, `connections.py`,
  `environment.py`, `events.py`, `mentions.py`, `overrides.py`, `project.py`, `roots.py`,
  `selfwake.py`, `subscriptions.py`, `unattended.py`, `unrouted.py`, `workspace_trust.py`,
  `cloud.py`
- `coworker/agents/`, `coworker/tools/`, `coworker/memory/`, `coworker/skills/`,
  `coworker/personas/`, `coworker/automation/`, `coworker/server/`, `coworker/tui/`,
  `coworker/web/`, `coworker/mcp/`

### Providers (Together transport only)
- `coworker/providers/__init__.py` *(prune: drop Anthropic/Gemini exports)*
- `coworker/providers/base.py`, `capabilities.py`, `errors.py`, `router.py`
- `coworker/providers/openai_provider.py` — **the Together transport, keep**
- `coworker/providers/registry.py` *(prune: keep only the `together` descriptor)*
- `coworker/providers/matrix.py` *(prune: keep only `together:*` model entries)*

### Connectors (Google Calendar only)
- `coworker/connectors/gcal_accounts.py` — GCal-specific, keep
- Shared infra — keep, **prune non-GCal content inside**:
  `__init__.py`, `accounts.py`, `adapters.py`, `attribution.py`, `base.py`, `catalog_copy.py`,
  `cli.py`, `config.py`, `descriptors.py`, `gateway.py`, `parked.py`, `relay_client.py`,
  `senders.py`, `setup.py`, `tool_defs.py`, `tools.py`, `integration_tools.py`
- `coworker/connectors/experimental/`

### GUI + packaging (Apple Silicon)
- `surfaces/gui/` (Tauri + React) *(prune Windows targets in `src-tauri/tauri.conf.json`)*
- `packaging/build_dmg.sh`, `openworker-server.spec`, `server_entry.py`,
  `make_update_manifest.py`, `setup_dev_env.sh`, DMG background assets
- `pyproject.toml`, `README.md`, `LICENSE`, `docs/`, `.github/`, `ui-mocks/`, `tests/`

---

## REMOVE — out of scope

### Providers
- `coworker/providers/anthropic_provider.py`
- `coworker/providers/gemini_provider.py`
- In `pyproject.toml`, drop deps: `anthropic`, `google-genai`
- In `registry.py` / `matrix.py` / `providers/__init__.py`: remove `openai` (as a
  user-facing key option), `anthropic`, `gemini`, `ollama`, and `fireworks` descriptors/exports;
  keep the OpenAI client wiring only as the Together backend.

### Connectors (everything except Google Calendar)
- `coworker/connectors/github_installs.py`, `github_relay.py`
- `coworker/connectors/gmail_accounts.py`, `email_tools.py`
- `coworker/connectors/hubspot_portals.py`
- `coworker/connectors/slack_addr.py`, `slack_directory.py`
- `coworker/connectors/browser_automation.py` *(and the `browser` extra + `playwright` dep)*
- `coworker/testing/fake_slack/`
- Prune all non-GCal entries out of `descriptors.py`, `tool_defs.py`, `integration_tools.py`,
  `catalog_copy.py`, `setup.py`, and the Slack surface handling in `server/app.py` / `cloud.py`.
- `messaging` extra in `pyproject.toml` (`python-telegram-bot`, `slack-bolt`, `aiohttp`) —
  remove unless a non-Slack path still needs it.

### Speech-to-text
- `stt/` (entire Rust crate)
- Remove the STT wiring from `surfaces/gui/src-tauri/Cargo.toml`, `Cargo.lock`,
  and `surfaces/gui/src-tauri/src/lib.rs`

### Windows / non-Apple-Silicon packaging
- `packaging/build_windows.ps1`
- `tzdata; sys_platform == 'win32'` dep in `pyproject.toml`
- Windows-specific blocks in `surfaces/gui/src-tauri/tauri.conf.json`

---

## Guardrails for future work

1. **Do not re-add** OpenAI/Anthropic/Gemini/Ollama/Fireworks as selectable providers.
   New models are added only as `together:*` entries in `matrix.py`.
2. **Do not re-add** any connector other than Google Calendar.
3. **Do not add** Windows/Linux/Intel packaging paths.
4. When editing shared connector files, preserve Google Calendar behavior; strip, never
   extend, other integrations.
5. After any REMOVE step, run `pytest` and delete now-orphaned tests under `tests/`.
