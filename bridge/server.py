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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                   id TEXT PRIMARY KEY, device TEXT, text TEXT, session TEXT,
                   state TEXT, result TEXT, error TEXT,
                   created_at REAL, updated_at REAL, expires_at REAL)"""
        )


def job_row(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    auth = request.headers.get("Authorization", "")
    if TOKEN and auth != f"Bearer {TOKEN}":
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
                "SELECT * FROM jobs WHERE device=? AND state='dispatched' ORDER BY created_at",
                (device,),
            ).fetchall()
    for row in rows:
        await ws.send_json({"t": "job", "id": row["id"], "text": row["text"],
                            "session": row["session"], "deadline": JOB_TTL})
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
    ws = _devices.get(job.get("device") or "default")
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
    device = str(body.get("device") or "default")
    session = str(body.get("session") or "")
    job_id = str(body.get("id") or uuid.uuid4().hex)
    now = time.time()
    async with _db_lock:
        with db() as conn:
            existing = job_row(conn, job_id)
            if existing is None:
                conn.execute(
                    "INSERT INTO jobs(id,device,text,session,state,created_at,updated_at,expires_at)"
                    " VALUES(?,?,?,?, 'queued', ?,?,?)",
                    (job_id, device, text, session, now, now, now + JOB_TTL))
                job = job_row(conn, job_id)
            else:
                job = existing
    if job and job["state"] == "queued":
        await _dispatch(job)
    wait = min(float(request.query.get("wait", "0") or 0), 300)
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
    wait = min(float(request.query.get("wait", "0") or 0), 300)
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


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "devices": sorted(_devices),
                              "device_count": len(_devices)})


async def reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for device, ws in list(_devices.items()):
            if ws.closed or now - _device_seen.get(device, 0) > DEAD_AFTER:
                _devices.pop(device, None)
                if not ws.closed:
                    await ws.close(code=4002, message=b"heartbeat timeout")
        async with _db_lock:
            with db() as conn:
                conn.execute("DELETE FROM jobs WHERE expires_at < ? AND state IN ('done','failed')", (now - 86400,))


def main() -> None:
    init_db()
    print(f"bridge: db ready at {DB_PATH}", flush=True)
    app = web.Application()
    app.router.add_get("/agent-bridge", ws_handler)
    app.router.add_post("/v1/jobs", create_job)
    app.router.add_get("/v1/jobs/{job_id}", get_job)
    app.router.add_get("/healthz", healthz)

    async def _start_background(_app: web.Application) -> None:
        _app["reaper"] = asyncio.create_task(reaper())

    app.on_startup.append(_start_background)
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("HERMES_BRIDGE_PORT", "8787")),
                print=None)


if __name__ == "__main__":
    main()
