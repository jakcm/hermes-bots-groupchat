#!/usr/bin/env bash
# Install the plugin inside DSH Go's local engine (run from the App's terminal).
#
#   curl -fsSL https://raw.githubusercontent.com/jakcm/hermes-bots-groupchat/main/scripts/phone-install.sh | sh
#
# Optional environment:
#   DSH_PROFILE   profile to install into (default: web)
#   BRIDGE_URL    wss://.../hermes/agent-bridge/?device=phone
#   BRIDGE_TOKEN  bridge device token (write it, then rotate it)
set -eu

PROFILE="${DSH_PROFILE:-web}"
NAME="dsh-hermes-bridge"
DEST="$HOME/.dsh/local/$NAME"
URL="https://github.com/jakcm/hermes-bots-groupchat/archive/refs/heads/main.tar.gz"
TMP="$(mktemp -d)"

echo "== download"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TMP/src.tgz"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMP/src.tgz" "$URL"
else
  echo "no curl/wget" >&2; exit 1
fi

echo "== extract"
mkdir -p "$DEST"
if command -v tar >/dev/null 2>&1; then
  tar xzf "$TMP/src.tgz" -C "$TMP"
else
  python3 - "$TMP/src.tgz" "$TMP" <<'PY'
import sys, tarfile
tarfile.open(sys.argv[1]).extractall(sys.argv[2])
PY
fi
cp -R "$TMP"/hermes-bots-groupchat-main/plugin/. "$DEST"/

echo "== dsh plugin add ($PROFILE)"
dsh plugin --profile "$PROFILE" add "$DEST"

echo "== activate bundle"
node - "$PROFILE" "$NAME" <<'JS'
const fs = require('fs'), os = require('os'), path = require('path')
const profile = process.argv[2] || 'web'
const name = process.argv[3]
const file = path.join(os.homedir(), '.dsh', 'profiles', profile, 'package.json')
const pkg = JSON.parse(fs.readFileSync(file, 'utf8'))
pkg.dsh = pkg.dsh || {}
pkg.dsh.profile = pkg.dsh.profile || {}
const bundles = pkg.dsh.profile.bundles || []
if (!bundles.includes(name)) bundles.push(name)
pkg.dsh.profile.bundles = bundles
fs.writeFileSync(file, JSON.stringify(pkg, null, 2) + '\n')
console.log('bundles:', bundles.join(', '))
JS

if [ -n "${BRIDGE_URL:-}" ]; then
  echo "== write config"
  node - "$BRIDGE_URL" "${BRIDGE_TOKEN:-}" "${BRIDGE_DEVICE:-phone}" <<'JS'
const fs = require('fs'), os = require('os'), path = require('path')
const [url, token, device] = process.argv.slice(2)
const file = path.join(os.homedir(), '.dsh', 'hermes-bridge.json')
fs.writeFileSync(file, JSON.stringify({ url, token, device, dsh_bin: 'dsh', profile: 'headless', timeout_s: 900 }, null, 2) + '\n', { mode: 0o600 })
console.log('config:', file)
JS
fi

echo "== done. Restart the engine (App: engine menu -> restart), then check the plugin list."
