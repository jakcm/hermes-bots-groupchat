#!/usr/bin/env python3
"""Self-test: bridge + mock device + MCP tool, no phone needed."""
import asyncio, json, os, subprocess, sys, tempfile
import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("TEST_PORT", "8799"))
TOKEN = "test-token"


async def mock_device(session):
    async with session.ws_connect(f"http://127.0.0.1:{PORT}/agent-bridge?device=default",
                                  headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        async for msg in ws:
            data = json.loads(msg.data)
            if data.get("t") == "job":
                await ws.send_json({"t": "ack", "id": data["id"]})
                await ws.send_json({"t": "result", "id": data["id"], "ok": True,
                                    "text": "pong:" + data["text"]})
            elif data.get("t") == "ping":
                await ws.send_json({"t": "pong"})


async def main():
    home = tempfile.mkdtemp(prefix="bridge-test-")
    env = dict(os.environ, HERMES_BRIDGE_HOME=home, HERMES_BRIDGE_TOKEN=TOKEN,
               HERMES_BRIDGE_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "bridge", "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dev = None
    try:
        await asyncio.sleep(1.2)
        async with aiohttp.ClientSession() as session:
            dev = asyncio.create_task(mock_device(session))
            await asyncio.sleep(0.4)

            async with session.post(f"http://127.0.0.1:{PORT}/v1/jobs?wait=10",
                                    json={"text": "hello", "session": "s1"}) as r:
                job = await r.json()
            assert job.get("state") == "done" and job.get("result") == "pong:hello", job
            print("bridge roundtrip OK:", job["result"])

            mcp_env = dict(os.environ, HERMES_BRIDGE_CONTROL=f"http://127.0.0.1:{PORT}")
            inp = "\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "dsh_turn", "arguments": {"text": "via-mcp", "timeout": 8}}}),
            ]) + "\n"
            out = await asyncio.to_thread(
                subprocess.run, [sys.executable, os.path.join(ROOT, "bridge", "mcp_dsh.py")],
                input=inp, capture_output=True, text=True, env=mcp_env, timeout=30)
            lines = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
            assert len(lines) == 3, out
            assert lines[1]["result"]["tools"][0]["name"] == "dsh_turn"
            assert "via-mcp" in lines[2]["result"]["content"][0]["text"], lines[2]
            print("mcp OK:", lines[2]["result"]["content"][0]["text"])
    finally:
        if dev:
            dev.cancel()
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
