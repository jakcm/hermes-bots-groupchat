# Phone install (DSH Go, CLI path)

The App's plugin market only lists published packages, so a custom plugin is
installed through the local engine's terminal.

1. In the App: **本机引擎 → 终端** (workspace terminal).
2. Run:

```sh
curl -fsSL https://raw.githubusercontent.com/jakcm/hermes-bots-groupchat/main/scripts/phone-install.sh | sh
```

With a bridge configured, prefer a **one-time pairing code** (no long-lived
secret is typed):

```sh
# on the cloud: prints an 8-char code, valid 10 minutes, single use
python3 bridge/pair.py --device phone
```

Then on the phone write only the URL and the code:

```json
{ "url": "wss://<host>/hermes/agent-bridge/?device=phone", "code": "ABCD2345" }
```

at `~/.dsh/hermes-bridge.json`. On first connect the plugin calls
`POST /hermes/agent-bridge/pair`, receives a device token, and stores it back in
the same file (mode 600).

3. Restart the engine.
4. Verify: `dsh --profile web --dump-config | grep hermes-bridge` should show the
   plugin row; the plugin log prints `[hermes-bridge] connected` after it reaches
   the bridge.

Notes:
- `dsh plugin add` only links the package; appending `dsh-hermes-bridge` to
  `~/.dsh/profiles/web/package.json` → `dsh.profile.bundles` activates it.
- The plugin has **no runtime dependencies** (Node built-ins only), so the CLI
  path needs no build-script approval.
- The token is a device credential: rotate it after installation.
