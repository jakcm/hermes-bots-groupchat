# Phone install (DSH Go, CLI path)

The App's plugin market only lists published packages, so a custom plugin is
installed through the local engine's terminal.

1. In the App: **本机引擎 → 终端** (workspace terminal).
2. Run:

```sh
curl -fsSL https://raw.githubusercontent.com/jakcm/hermes-bots-groupchat/main/scripts/phone-install.sh | sh
```

With a bridge configured:

```sh
BRIDGE_URL='wss://<host>/hermes/agent-bridge/?device=phone' \
BRIDGE_TOKEN='<token>' \
sh -c "$(curl -fsSL https://raw.githubusercontent.com/jakcm/hermes-bots-groupchat/main/scripts/phone-install.sh)"
```

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
