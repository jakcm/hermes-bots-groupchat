#!/usr/bin/env python3
"""Generate a one-time pairing code for a phone (prints code only; TTL 10 min)."""
from __future__ import annotations

import argparse
import secrets
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="phone")
    ap.add_argument("--ttl", type=int, default=600)
    ap.add_argument("--db", default=str(ROOT / "queue.db"))
    args = ap.parse_args()

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    now = time.time()
    conn = sqlite3.connect(args.db)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS pair_codes(code TEXT PRIMARY KEY, device TEXT, expires_at REAL, used_at REAL)")
        # one live code per device
        conn.execute("DELETE FROM pair_codes WHERE device=? AND used_at IS NULL", (args.device,))
        conn.execute("INSERT INTO pair_codes(code, device, expires_at) VALUES(?,?,?)",
                     (code, args.device, now + args.ttl))
    print(code)


if __name__ == "__main__":
    main()
