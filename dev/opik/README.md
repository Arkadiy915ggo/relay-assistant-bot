# Development-only Opik exporter

This is an independently installable Python distribution, `relay-assistant-bot-opik`.
It is maintained in this checkout, but is **not part of the bot wheel or normal runtime
dependencies**. The `tg_summary_bot` package only exposes generic completed-span hooks.
The adapter can later move to its own repository without changing bot feature modules.

## Setup

From the repository root:

```bash
./run.sh install-dev
# Or: .venv/bin/python -m pip install -e . -e ./dev/opik
```

Installing the adapter or having the Opik SDK installed does not enable it. Both an
explicit flag and nonblank key are required:

```env
TRACING_ENABLED=true
TRACING_BACKEND=opik
TRACING_API_KEY=your_key
TRACING_WORKSPACE=your_workspace
TRACING_PROJECT_NAME=telegram-summary-bot
TRACING_CAPTURE_CONTENT=false
# TRACING_ENDPOINT=http://localhost:5173/api/
```

Use process environment or `.env`; restart the bot after changing configuration.
Legacy `OPIK_*` names are fallback inputs, but `~/.opik.config` is never used.
To disable, set `TRACING_ENABLED=false`, empty `TRACING_API_KEY`, or uninstall
`relay-assistant-bot-opik`. No-key activation is also disabled for self-hosted servers;
provide an explicit placeholder only if the server accepts unauthenticated clients.

## Export contract

- Entry point: `tg_summary_bot.observability / opik`.
- No SDK import at module import time, nor before the key gate.
- SDK Sentry reporting disabled and SDK home config redirected to `os.devnull` **before import**.
- Pinned Opik async REST client; no SDK global tracker, connection-monitor threads or implicit integrations.
- One async exporter task with a fresh context; at most 128 queued records and batches of 64.
- Completed full span/root payloads avoid create/update batching races. Every root has
  an anchor span; child parent IDs and trace IDs come from the generic runtime context.
- No application exceptions enter SDK code. Exported errors contain safe codes/types only.
- Content capture defaults off; the adapter additionally strips input/output when off.
- Five-second request deadline, zero SDK retries. The first export error disables
  further sends for this process and produces one safe host warning. Restart to retry.
- Shutdown flush is bounded to three seconds; in-flight requests are cancelled and HTTP is closed.

## Checks

```bash
.venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=dev/opik/src .venv/bin/python -m unittest discover -s dev/opik/tests -v
```

The adapter suite includes a pinned-SDK contract check with `httpx.MockTransport`:
real request serialization, explicit auth/workspace, telemetry disabled, no DNS or live HTTP.

The live ingestion smoke is separate and **requires explicit network consent via `--send`**:

```bash
# Export TRACING_API_KEY / TRACING_WORKSPACE in the shell first.
.venv/bin/python dev/opik/voice_trace_smoke.py --send
```

It does not load `.env`, home SDK config, production logs/databases or Telegram
credentials. It uses fake models, ffmpeg and Telegram, temporary storage, and tags
synthetic traces. It is not evidence of real GPU/Telegram latency.

## Personal OpenCode MCP

MCP is a developer-side tool, not the bot exporter. The repository does not start or
install it. If needed, merge this example into your **personal**
`~/.config/opencode/opencode.json` (preserve other settings):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "opik-mcp": {
      "type": "local",
      "command": ["uvx", "opik-mcp"],
      "enabled": false,
      "timeout": 120000
    }
  }
}
```

Configure MCP credentials through its own process environment; do not commit keys.
Enable it there only when needed. `uvx` must be on `PATH` and can contact package
indexes when preparing the tool. Quit and restart OpenCode after config changes;
an already-running session keeps its previous MCP configuration.
