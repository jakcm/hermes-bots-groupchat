# hermes-bots-groupchat

Hermes group chat ↔ phone DeepSeek Harness bot, over an **outbound-only WebSocket**.
The phone never exposes a port; it dials the cloud and pulls tasks from a durable queue.

```
hermes group room (G2)
   └─ profile: dshphone ── MCP tool dsh_turn ──> bridge control API (127.0.0.1:8788)
                                                     │
                                        bridge (127.0.0.1:8787) + SQLite queue
                                                     │  443 /hermes/agent-bridge (WSS)
                                                     ▼
                                        phone DSH plugin ── dsh headless / engine
```

## Layout

| path | purpose |
|---|---|
| `bridge/server.py` | device WebSocket + job queue + control API |
| `bridge/mcp_dsh.py` | MCP stdio server exposing `dsh_turn` |
| `plugin/` | DSH plugin (bundle: `cordis.patch.yml` + `index.js`) |
| `tests/e2e_local.py` | end-to-end test with a mock device (no phone needed) |
| `scripts/` | deploy / build helpers |

## Protocol (single-line JSON frames)

| frame | direction | meaning |
|---|---|---|
| `hello` | server → device | connection accepted |
| `job` | server → device | `{id, text, session, deadline}` |
| `ack` | device → server | accepted, now running |
| `progress` | device → server | optional status |
| `result` | device → server | `{id, ok, text}` |
| `ping` / `pong` | both | liveness |

Jobs are persisted in SQLite; a job is delivered at least once and is idempotent by `id`.

## Local test

```sh
python3 tests/e2e_local.py
```

Requires `aiohttp` (already present in the Hermes runtime venv).

## Deploy (cloud)

1. `scripts/deploy.sh` installs the bridge as a systemd unit and adds the nginx
   location (token in `~/.hermes/.env`, never committed).
2. `hermes mcp add dsh-bridge --command <repo>/bridge/mcp_dsh.py`
3. `hermes profile create dshphone` (or reuse an existing profile) and enable the MCP server.

## How a job runs on the phone

The plugin lives inside the DSH engine, so it does not spawn `dsh headless` (that
profile has no model credentials). It drives the engine directly:

1. `sessionController.create({ cwd })` → `sessionId`
2. `sessionController.prompt({ sessionId, mode: 'queue', content: [...] })`
   — the same call DS-Bot's `/api/bot` `send` handler uses
3. the reply is read from the `assistant/message` event:
   `event.data.message.content[].text` (with a `sessionController.page` poll as
   a fallback)

Verified on device: `1+1=?` → `2`.

## Phone

The plugin is installable only through DSH's own plugin manager (the App market
indexes published packages; local paths are rejected). Build a tarball with
`scripts/build_plugin.sh` and install it from a published source or the engine's
CLI.

## Security

- The bridge token grants task execution on the phone: rotate it, keep it in
  `~/.hermes/.env` (600) and the phone plugin config only.
- The bridge binds to `127.0.0.1`; nginx is the only public surface.
- Never commit `.env`, tokens, or built tarballs (see `.gitignore`).

## 50-minute task profile (3000s)

| layer | value |
|---|---|
| `~/.hermes/config.yaml` → `timeouts.mcp.tool_call` | `3050` |
| `hermes-dashboard.service` → `HERMES_AGENT_TIMEOUT` | `3000` (room turn = `3030`) |
| `bridge/mcp_dsh.py` default `timeout` | `2970` |
| bridge `/v1/jobs` long-poll cap | `3100` |
| phone `~/.dsh/hermes-bridge.json` → `timeout_s` | `3010` |
| bridge `JOB_TTL` | `3600` (10 min headroom) |

Restart `hermes-dashboard` (env change) and `hermes-bridge` (code change) after editing.
`HERMES_AGENT_TIMEOUT` is the gateway's *inactivity* timeout and applies to every
profile, so stuck turns elsewhere are also reaped only after 50 minutes.
