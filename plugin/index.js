/**
 * dsh-hermes-bridge — pull jobs from a Hermes bridge over an outbound WebSocket and
 * run them with the local DeepSeek Harness (`dsh headless --json`).
 *
 * Config (first found wins):
 *   $HERMES_BRIDGE_CONFIG
 *   ~/.dsh/hermes-bridge.json
 *   config.json next to this file
 *
 * First run may use a one-time pairing code instead of a token:
 *   { "url": "wss://host/hermes/agent-bridge/?device=phone", "code": "ABCD2345" }
 * The plugin exchanges the code for a device token, stores it in the same file
 * (mode 600), and reuses it afterwards.
 */
import { spawn } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'hermes-bridge'
export const inject = []

const HERE = dirname(fileURLToPath(import.meta.url))
const DEFAULTS = { device: 'phone', dsh_bin: 'dsh', profile: 'headless', timeout_s: 900 }
const ACTIVE = new Set()

function configPaths() {
  return [process.env.HERMES_BRIDGE_CONFIG, join(homedir(), '.dsh', 'hermes-bridge.json'),
          join(HERE, 'config.json')].filter(Boolean)
}

function loadConfig() {
  for (const path of configPaths()) {
    try {
      return { path, config: { ...DEFAULTS, ...JSON.parse(readFileSync(path, 'utf8')) } }
    } catch { /* try next */ }
  }
  return { path: configPaths()[1], config: { ...DEFAULTS } }
}

function saveConfig(path, cfg) {
  const { path: _ignore, ...clean } = cfg
  writeFileSync(path, JSON.stringify(clean, null, 2) + '\n', { mode: 0o600 })
}

function pairUrl(wsUrl) {
  const u = new URL(wsUrl)
  u.protocol = u.protocol === 'wss:' ? 'https:' : 'http:'
  u.pathname = u.pathname.replace(/\/?$/, '/') + 'pair'
  u.search = ''
  return u.toString()
}

async function ensureToken(state) {
  const { config: cfg } = state
  if (cfg.token) return cfg.token
  if (!cfg.code) return ''
  const res = await fetch(pairUrl(cfg.url), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: cfg.code, device: cfg.device }),
  })
  if (!res.ok) throw new Error(`pair failed: ${res.status} ${await res.text()}`)
  const data = await res.json()
  if (!data.token) throw new Error('pair failed: no token')
  cfg.token = data.token
  delete cfg.code
  try { saveConfig(state.path, cfg) } catch (e) { console.error('[hermes-bridge] cannot persist token:', e?.message || e) }
  return cfg.token
}

function runDsh(cfg, text) {
  const timeoutMs = Math.max(1, Number(cfg.timeout_s) || 900) * 1000
  return new Promise(resolve => {
    const args = []
    if (cfg.profile) args.push('--profile', cfg.profile)
    args.push('headless', '--json', text)
    const child = spawn(cfg.dsh_bin || 'dsh', args, {
      cwd: cfg.cwd || homedir(),
      env: { ...process.env, ...(cfg.dsh_env || {}) },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    let err = ''
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs)
    child.stdout.on('data', chunk => { out += chunk.toString() })
    child.stderr.on('data', chunk => { err = (err + chunk.toString()).slice(-4000) })
    child.on('error', e => { clearTimeout(timer); resolve({ ok: false, text: String(e.message || e) }) })
    child.on('close', code => {
      clearTimeout(timer)
      const final = out.split('\n').map(line => {
        try { return JSON.parse(line) } catch { return null }
      }).filter(Boolean).reverse().find(ev => ev.type === 'final')
      const answer = final?.text ?? final?.message ?? final?.content ?? ''
      if (code === 0 && answer) resolve({ ok: true, text: String(answer) })
      else resolve({ ok: false, text: String(answer || err || `dsh exited ${code}`) })
    })
  })
}

export function apply() {
  const state = loadConfig()
  const cfg = state.config
  if (!cfg.url) {
    console.error('[hermes-bridge] no config (url missing); plugin idle')
    return
  }
  let stopped = false
  let backoff = 1000

  const connect = async () => {
    if (stopped) return
    let token = ''
    try {
      token = await ensureToken(state)
    } catch (e) {
      console.error('[hermes-bridge]', e?.message || e)
      setTimeout(connect, backoff)
      backoff = Math.min(backoff * 2, 60000)
      return
    }
    const ws = new WebSocket(cfg.url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
    ws.addEventListener('open', () => {
      backoff = 1000
      ws.send(JSON.stringify({ t: 'hello', device: cfg.device, ver: 1 }))
      console.error('[hermes-bridge] connected', cfg.url)
    })
    ws.addEventListener('message', async ev => {
      let msg
      try { msg = JSON.parse(String(ev.data)) } catch { return }
      if (msg.t === 'ping') { ws.send(JSON.stringify({ t: 'pong' })); return }
      if (msg.t !== 'job' || ACTIVE.has(msg.id)) return
      ACTIVE.add(msg.id)
      try {
        ws.send(JSON.stringify({ t: 'ack', id: msg.id }))
        const res = await runDsh(cfg, msg.text)
        ws.send(JSON.stringify({ t: 'result', id: msg.id, ok: res.ok, text: res.text }))
      } catch (e) {
        try { ws.send(JSON.stringify({ t: 'result', id: msg.id, ok: false, text: String(e) })) } catch { /* gone */ }
      } finally {
        ACTIVE.delete(msg.id)
      }
    })
    const retry = () => {
      if (stopped) return
      setTimeout(connect, backoff)
      backoff = Math.min(backoff * 2, 60000)
    }
    ws.addEventListener('close', retry)
    ws.addEventListener('error', () => { try { ws.close() } catch { /* noop */ } })
  }

  connect()
  return () => { stopped = true }
}
