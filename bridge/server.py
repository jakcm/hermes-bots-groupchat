#!/usr/bin/env python3
"""Hermes <-> phone agent bridge.

Device side:  WS  /agent-bridge   (Authorization: Bearer <token>)
Control side: POST /v1/jobs, GET /v1/jobs/{id}, GET /healthz   (127.0.0.1 only)

Jobs are persisted in SQLite so a phone that is offline or asleep still gets the
task on reconnect (at-least-once delivery, idempotent by job id on the phone).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web, WSMsgType

ROOT = Path(os.environ.get("HERMES_BRIDGE_HOME", Path.home() / ".hermes" / "bridge"))
DB_PATH = ROOT / "queue.db"
TOKEN = os.environ.get("HERMES_BRIDGE_TOKEN", "").strip()
JOB_TTL = 3600
PING_EVERY = 25.0
DEAD_AFTER = 90.0

_db_lock = asyncio.Lock()


def _hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def _authorized(token: str) -> bool:
    if TOKEN and token == TOKEN:
        return True
    if not token:
        return False
    with db() as conn:
        row = conn.execute("SELECT 1 FROM devices WHERE token_hash=? AND revoked_at IS NULL",
                           (_hash(token),)).fetchone()
    return row is not None
_devices: dict[str, web.WebSocketResponse] = {}
_device_seen: dict[str, float] = {}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS devices(
                   token_hash TEXT PRIMARY KEY, device TEXT, created_at REAL, revoked_at REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pair_codes(
                   code TEXT PRIMARY KEY, device TEXT, expires_at REAL, used_at REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs_meta_marker(x INTEGER)""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                   id TEXT PRIMARY KEY, device TEXT, text TEXT, session TEXT,
                   state TEXT, result TEXT, error TEXT,
                   created_at REAL, updated_at REAL, expires_at REAL, deadline_at REAL)"""
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "deadline_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN deadline_at REAL")


def job_row(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query.get("token", "")
    if not _authorized(token):
        return web.Response(status=401, text="bad token")
    device = request.query.get("device") or request.headers.get("X-Device") or "default"
    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=1024 * 1024)
    await ws.prepare(request)
    old = _devices.get(device)
    if old is not None and not old.closed:
        await old.close(code=4001, message=b"replaced by a newer connection")
    _devices[device] = ws
    _device_seen[device] = time.time()
    await ws.send_json({"t": "hello", "ver": 1, "device": device})
    # Re-deliver anything still dispatched but unacked.
    async with _db_lock:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state='dispatched'"
                " AND (device='' OR device='default' OR device=?) ORDER BY created_at",
                (device,),
            ).fetchall()
    for row in rows:
        await ws.send_json({"t": "job", "id": row["id"], "text": row["text"],
                            "session": row["session"], "deadline": JOB_TTL})
    async with _db_lock:
        with db() as conn:
            queued = conn.execute(
                "SELECT * FROM jobs WHERE state='queued' AND (device='' OR device=? OR device='default') ORDER BY created_at LIMIT 20",
                (device,)).fetchall()
    for row in queued:
        await ws.send_json({"t": "job", "id": row["id"], "text": row["text"],
                            "session": row["session"], "deadline": JOB_TTL})
        async with _db_lock:
            with db() as conn:
                conn.execute("UPDATE jobs SET state='dispatched', device=?, updated_at=? WHERE id=?",
                             (device, time.time(), row["id"]))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            _device_seen[device] = time.time()
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            t = data.get("t")
            if t == "pong":
                continue
            if t == "ack":
                async with _db_lock:
                    with db() as conn:
                        conn.execute("UPDATE jobs SET state='running', updated_at=? WHERE id=?",
                                     (time.time(), data.get("id")))
            elif t == "result":
                async with _db_lock:
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET state=?, result=?, error=?, updated_at=? WHERE id=?",
                            ("done" if data.get("ok") else "failed", str(data.get("text") or ""),
                             str(data.get("error") or ""), time.time(), data.get("id")))
            elif t == "progress":
                continue
    finally:
        if _devices.get(device) is ws:
            _devices.pop(device, None)
    return ws


async def _dispatch(job: dict[str, Any]) -> bool:
    wanted = job.get("device") or ""
    ws = _devices.get(wanted)
    if ws is None and wanted in ("", "default") and len(_devices) == 1:
        ws = next(iter(_devices.values()))
    if ws is None or ws.closed:
        return False
    await ws.send_json({"t": "job", "id": job["id"], "text": job["text"],
                        "session": job["session"], "deadline": JOB_TTL})
    async with _db_lock:
        with db() as conn:
            conn.execute("UPDATE jobs SET state='dispatched', updated_at=? WHERE id=? AND state='queued'",
                         (time.time(), job["id"]))
    return True


async def create_job(request: web.Request) -> web.Response:
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    device = str(body.get("device") or "")
    session = str(body.get("session") or "")
    job_id = str(body.get("id") or uuid.uuid4().hex)
    now = time.time()
    async with _db_lock:
        with db() as conn:
            existing = job_row(conn, job_id)
            if existing is None:
                try:
                    budget = float(body.get("timeout") or JOB_TTL)
                except (TypeError, ValueError):
                    budget = JOB_TTL
                budget = max(5.0, min(budget, JOB_TTL))
                conn.execute(
                    "INSERT INTO jobs(id,device,text,session,state,created_at,updated_at,expires_at,deadline_at)"
                    " VALUES(?,?,?,?, 'queued', ?,?,?,?)",
                    (job_id, device, text, session, now, now, now + JOB_TTL, now + budget + 10))
                job = job_row(conn, job_id)
            else:
                job = existing
    if job and job["state"] == "queued":
        await _dispatch(job)
    wait = min(float(request.query.get("wait", "0") or 0), 3100)
    if wait:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            async with _db_lock:
                with db() as conn:
                    job = job_row(conn, job_id)
            if job and job["state"] in ("done", "failed"):
                break
            await asyncio.sleep(0.5)
    return web.json_response(job or {"error": "missing"})


async def get_job(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    wait = min(float(request.query.get("wait", "0") or 0), 3100)
    deadline = time.monotonic() + wait
    while True:
        async with _db_lock:
            with db() as conn:
                job = job_row(conn, job_id)
        if job is None:
            return web.json_response({"error": "not found"}, status=404)
        if job["state"] in ("done", "failed") or time.monotonic() >= deadline:
            return web.json_response(job)
        await asyncio.sleep(0.5)


async def pair(request: web.Request) -> web.Response:
    import secrets
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "json body required"}, status=400)
    code = str(body.get("code") or "").strip().upper()
    device = str(body.get("device") or "")[:64]
    if not code:
        return web.json_response({"error": "code required"}, status=400)
    now = time.time()
    async with _db_lock:
        with db() as conn:
            row = conn.execute("SELECT * FROM pair_codes WHERE code=?", (code,)).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] < now:
                return web.json_response({"error": "invalid or expired code"}, status=403)
            token = secrets.token_urlsafe(32)
            conn.execute("UPDATE pair_codes SET used_at=? WHERE code=?", (now, code))
            conn.execute("INSERT OR REPLACE INTO devices(token_hash, device, created_at) VALUES(?,?,?)",
                         (_hash(token), device, now))
    return web.json_response({"token": token, "device": device})


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "devices": sorted(_devices),
                              "device_count": len(_devices)})


_gone_since: dict[str, float] = {}


async def reaper() -> None:
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for device, ws in list(_devices.items()):
            if ws.closed or now - _device_seen.get(device, 0) > DEAD_AFTER:
                _devices.pop(device, None)
                if not ws.closed:
                    await ws.close(code=4002, message=b"heartbeat timeout")
            else:
                try:
                    await ws.send_json({"t": "ping"})
                except Exception:
                    _devices.pop(device, None)
        async with _db_lock:
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET state='failed', error='deadline exceeded', updated_at=?"
                    " WHERE state IN ('queued','dispatched','running')"
                    " AND COALESCE(deadline_at, expires_at) < ?", (now, now))
                # dispatched but never acked for 60s -> requeue once
                stale = conn.execute(
                    "SELECT * FROM jobs WHERE state='dispatched' AND updated_at < ? LIMIT 5",
                    (now - 60,)).fetchall()
                for row in stale:
                    conn.execute("UPDATE jobs SET state='queued', updated_at=? WHERE id=?", (now, row["id"]))
                rows = conn.execute(
                    "SELECT DISTINCT device FROM jobs WHERE state IN ('dispatched','running')").fetchall()
                for row in rows:
                    dev = row["device"] or ""
                    live = dev in _devices or (dev in ("", "default") and len(_devices) == 1)
                    if live:
                        _gone_since.pop(dev, None)
                        continue
                    since = _gone_since.setdefault(dev, now)
                    if now - since >= 120:
                        conn.execute(
                            "UPDATE jobs SET state='failed', error=?, updated_at=?"
                            " WHERE device=? AND state IN ('dispatched','running')",
                            (f"device '{dev}' offline for {int(now - since)}s", now, dev))
                        _gone_since.pop(dev, None)
                conn.execute("DELETE FROM jobs WHERE expires_at < ? AND state IN ('done','failed')", (now - 86400,))


def main() -> None:
    init_db()
    print(f"bridge: db ready at {DB_PATH}", flush=True)
    app = web.Application()
    app.router.add_get("/agent-bridge", ws_handler)
    app.router.add_get("/agent-bridge/", ws_handler)
    app.router.add_post("/v1/jobs", create_job)
    app.router.add_get("/v1/jobs/{job_id}", get_job)
    app.router.add_post("/pair", pair)
    app.router.add_get("/healthz", healthz)

    async def _start_background(_app: web.Application) -> None:
        _app["reaper"] = asyncio.create_task(reaper())

    app.on_startup.append(_start_background)
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("HERMES_BRIDGE_PORT", "8787")),
                print=None)


if __name__ == "__main__":
    main()
