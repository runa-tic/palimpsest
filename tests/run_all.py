#!/usr/bin/env python3
"""Run every tests/test_*.py with this interpreter and summarise.

One command on every OS (the README's shell loop is bash-only, and macOS has no `timeout`):

  python tests/run_all.py            # all scripts
  python tests/run_all.py ledger rlm # only scripts whose name contains one of the words

Each script runs in its own process under a 10-minute limit; the exit code is the number of
scripts that failed or timed out (0 = green). A script's full output is printed only when it fails.
"""
from __future__ import annotations
import subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT_S = 600


def main() -> int:
    words = sys.argv[1:]
    scripts = [p for p in sorted(HERE.glob("test_*.py")) if not words or any(w in p.name for w in words)]
    bad = 0
    for p in scripts:
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, str(p)], cwd=HERE, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=LIMIT_S)
            out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc = "TIMEOUT"
        last = next((l for l in reversed(out.splitlines()) if l.strip()), "")
        print(f"{'ok  ' if rc == 0 else 'FAIL'} {p.name:<42} {time.monotonic() - t0:6.1f}s  {last[:90]}")
        if rc != 0:
            bad += 1
            print("\n".join("     | " + l for l in out.splitlines()[-40:]))
    print(f"\n{len(scripts) - bad}/{len(scripts)} scripts passed")
    return bad


if __name__ == "__main__":
    sys.exit(main())
