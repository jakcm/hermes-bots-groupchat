/**
 * dsh-hermes-bridge — pull jobs from a Hermes bridge over an outbound WebSocket and
 * run them inside the local DeepSeek Harness engine (same credentials/model as the app).
 *
 * Config: $HERMES_BRIDGE_CONFIG | ~/.dsh/hermes-bridge.json | config.json (next to this file)
 *   { "url": "wss://host/hermes/agent-bridge/?device=phone",
 *     "code": "ABCD2345"            // first run only; exchanged for a device token
 *     "token": "...",               // written back automatically
 *     "cwd": "/root", "timeout_s": 300 }
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createUserMessage, expandAssistantStream } from '@deepseek-ai/dsh-llm'

export const name = 'hermes-bridge'
export const inject = ['sessionController']

const HERE = dirname(fileURLToPath(import.meta.url))
const DEFAULTS = { device: 'phone', timeout_s: 300, cwd: '' }
const ACTIVE = new Set()
const PENDING = new Map() // sessionId -> { chunks, resolve, timer }
const curPoll = new Map() // sessionId -> interval

function configPaths() {
  return [process.env.HERMES_BRIDGE_CONFIG, join(homedir(), '.dsh', 'hermes-bridge.json'),
          join(HERE, 'config.json')].filter(Boolean)
}

function loadConfig() {
  for (const path of configPaths()) {
    try { return { path, config: { ...DEFAULTS, ...JSON.parse(readFileSync(path, 'utf8')) } } } catch { /* next */ }
  }
  return { path: configPaths()[1], config: { ...DEFAULTS } }
}

function saveConfig(path, cfg) {
  const { path: _p, ...clean } = cfg
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
  const cfg = state.config
  if (cfg.token) return cfg.token
  if (!cfg.code) return ''
  const res = await fetch(pairUrl(cfg.url), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
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

function runInEngine(ctx, cfg, text) {
  const timeoutMs = Math.max(1, Number(cfg.timeout_s) || 300) * 1000
  return (async () => {
    const cwd = cfg.cwd || process.cwd()
    const created = await ctx.sessionController.create({ cwd })
    const sessionId = created?.sessionId || created?.id
    if (!sessionId) return { ok: false, text: 'sessionController.create returned no sessionId' }
    console.error('[hermes-bridge] session', sessionId)

    const done = new Promise(resolve => {
      const timer = setTimeout(() => { PENDING.delete(sessionId); resolve({ ok: false, text: 'timeout' }) }, timeoutMs)
      PENDING.set(sessionId, { chunks: [], resolve, timer })
      // Fallback for engines whose turn events differ: poll the session page.
      const poll = setInterval(async () => {
        const cur = PENDING.get(sessionId)
        if (!cur) { clearInterval(poll); return }
        try {
          const page = await ctx.sessionController.page({ address: { kind: 'session', sessionId }, maxMessages: 60 })
          const events = (page?.records || []).map(r => r.event).filter(Boolean)
          const parts = []
          for (const event of events) {
            if (event.type !== 'assistant/message') continue
            for (const { chunk } of expandAssistantStream(event.data?.stream ?? [])) {
              if (chunk?.type === 'text-delta' && chunk.text) parts.push(chunk.text)
            }
          }
          const answer = parts.join('').trim()
          if (answer) {
            clearInterval(poll); clearTimeout(cur.timer); PENDING.delete(sessionId)
            cur.resolve({ ok: true, text: answer })
          }
        } catch { /* page not ready */ }
      }, 800)
      curPoll.set(sessionId, poll)
    })

    try {
      // The same call DS-Bot's /api/bot handler uses for "send" (channel.js).
      await ctx.sessionController.prompt({
        requestId: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        sessionId,
        mode: 'queue',
        content: [{ type: 'text', text }],
      }, AbortSignal.timeout(timeoutMs))
      console.error('[hermes-bridge] prompt accepted', sessionId)
    } catch (e) {
      const pending = PENDING.get(sessionId)
      if (pending) { clearTimeout(pending.timer); PENDING.delete(sessionId) }
      const p = curPoll.get(sessionId); if (p) { clearInterval(p); curPoll.delete(sessionId) }
      return { ok: false, text: String(e?.message || e) }
    }
    return await done
  })()
}

export function apply(ctx) {
  const state = loadConfig()
  const cfg = state.config
  if (!cfg.url) {
    console.error('[hermes-bridge] no config (url missing); plugin idle')
    return
  }

  ctx.on('session/event', (session, event) => {
    const pending = PENDING.get(session?.id)
    if (!pending) return
    if (event?.type === 'assistant/message' || event?.type === 'assistant/attempt') {
      try {
        for (const { chunk } of expandAssistantStream(event.data?.stream ?? [])) {
          if (chunk?.type === 'text-delta' && chunk.text) pending.chunks.push(chunk.text)
        }
      } catch { /* ignore stream shape drift */ }
    }
    if (event?.type === 'turn/end') {
      clearTimeout(pending.timer)
      const p = curPoll.get(session.id); if (p) { clearInterval(p); curPoll.delete(session.id) }
      PENDING.delete(session.id)
      const text = pending.chunks.join('').trim()
      pending.resolve(text ? { ok: true, text } : { ok: false, text: 'empty reply' })
    }
  })

  let stopped = false
  let backoff = 1000
  const connect = async () => {
    if (stopped) return
    let token = ''
    try { token = await ensureToken(state) } catch (e) {
      console.error('[hermes-bridge]', e?.message || e)
      setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 60000); return
    }
    const ws = new WebSocket(cfg.url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
    ws.addEventListener('open', () => {
      backoff = 1000
      ws.send(JSON.stringify({ t: 'hello', device: cfg.device, ver: 1 }))
      console.error('[hermes-bridge] connected')
    })
    ws.addEventListener('message', async ev => {
      let msg
      try { msg = JSON.parse(String(ev.data)) } catch { return }
      if (msg.t === 'ping') { ws.send(JSON.stringify({ t: 'pong' })); return }
      if (msg.t !== 'job' || ACTIVE.has(msg.id)) return
      ACTIVE.add(msg.id)
      try {
        ws.send(JSON.stringify({ t: 'ack', id: msg.id }))
        const res = await runInEngine(ctx, cfg, msg.text)
        ws.send(JSON.stringify({ t: 'result', id: msg.id, ok: res.ok, text: res.text }))
      } catch (e) {
        try { ws.send(JSON.stringify({ t: 'result', id: msg.id, ok: false, text: String(e?.message || e) })) } catch { /* gone */ }
      } finally { ACTIVE.delete(msg.id) }
    })
    const retry = () => { if (!stopped) { setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 60000) } }
    ws.addEventListener('close', retry)
    ws.addEventListener('error', () => { try { ws.close() } catch { /* noop */ } })
  }
  connect()
  return () => { stopped = true }
}
