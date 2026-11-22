#!/usr/bin/env python3
"""Minimal MCP stdio server: one tool, dsh_turn, backed by the local bridge."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BRIDGE = os.environ.get("HERMES_BRIDGE_CONTROL", "http://127.0.0.1:8787")
TOOL = {
    "name": "dsh_turn",
    "description": "REQUIRED for every user message: forward the user's text verbatim to the phone's DeepSeek Harness bot and return its answer. Never answer from your own knowledge; if this tool fails, reply with the tool's error text.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Task text for the phone agent."},
            "session": {"type": "string", "description": "Optional session key for continuity."},
            "timeout": {"type": "integer", "description": "Seconds to wait (default 2970, i.e. ~50 minutes)."},
        },
        "required": ["text"],
    },
}


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BRIDGE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get(path: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(BRIDGE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def dsh_turn(args: dict) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    timeout = int(args.get("timeout") or 2970)
    job = post(f"/v1/jobs?wait=5", {"text": text, "session": args.get("session") or ""})
    job_id = job.get("id")
    if not job_id:
        return "bridge error: no job id"
    deadline_wait = max(timeout - 5, 1)
    job = get(f"/v1/jobs/{job_id}?wait={deadline_wait}", timeout=deadline_wait + 15)
    if job.get("state") != "done":
        return f"[dsh {job.get('state')}] {job.get('error') or 'no result (phone offline?)'}"
    return str(job.get("result") or "")


def reply(msg_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            reply(rid, {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "dsh-bridge", "version": "0.1.0"}})
        elif method == "tools/list":
            reply(rid, {"tools": [TOOL]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                out = dsh_turn(params.get("arguments") or {})
                reply(rid, {"content": [{"type": "text", "text": out}], "isError": False})
            except Exception as exc:  # noqa: BLE001
                reply(rid, {"content": [{"type": "text", "text": f"dsh_turn failed: {exc}"}], "isError": True})
        elif method == "notifications/initialized":
            continue
        elif rid is not None:
            reply(rid, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()
