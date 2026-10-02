#!/usr/bin/env python3
"""Run every tests/test_*.py with this interpreter and summarise.

One command, no bash or `timeout` needed (macOS has no `timeout`); run on macOS and Windows 11 so far:

  python tests/run_all.py                              # all scripts
  python tests/run_all.py ledger rlm                   # only scripts whose name contains one of the words
  python tests/run_all.py tests/test_state_ledger.py   # a path names its script

Each script runs in its own process under a 10-minute limit; the exit code is 0 when every script
passed and 1 when any failed or timed out (the summary line says how many). A word that matches no
script is a usage error: exit 2, and nothing runs. A script's full output is printed only when it
fails.
"""
from __future__ import annotations
import codecs, locale, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT_S = 600


def child_encoding() -> str:
    """What a child Python writes to a pipe, so its output is read in that encoding.

    Asked of a child rather than worked out here: a child inherits the environment
    (PYTHONIOENCODING, PYTHONUTF8, the locale) but not this interpreter's -X options, so a runner
    started with `-X utf8` read its children's cp1251 as UTF-8 (review, 2026-10-02), and Python
    turns UTF-8 mode on by itself under a C locale, so the flags alone cannot tell either. The
    children are left on the code page rather than forced to UTF-8 so the suite keeps running the
    code paths a stock install runs, not those of a UTF-8 mode the user's machine does not have."""
    try:
        r = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
                           capture_output=True, timeout=60)
        return codecs.lookup(r.stdout.decode("ascii", "replace").strip()).name
    except (OSError, subprocess.SubprocessError, LookupError):
        return locale.getpreferredencoding(False)


def main() -> int:
    # A redirected or piped run writes in the code page (cp1252 on an ANSI Windows), and one
    # character it lacks must not abort the whole suite (Codex review, 2026-10-02).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    words = sys.argv[1:]
    if "-h" in words or "--help" in words:
        print(__doc__)
        return 0
    # A word is matched by its file name, so tests/test_x.py names test_x.py. A word that matched
    # nothing (a typo, an option, a path) ran 0 scripts and exited 0: a green run that tested
    # nothing (review, 2026-10-02).
    every = sorted(HERE.glob("test_*.py"))
    names = [Path(w).name for w in words]
    unmatched = [w for w, n in zip(words, names) if not any(n in p.name for p in every)]
    if unmatched:
        print(f"run_all.py: no tests/test_*.py matches {', '.join(map(repr, unmatched))}; "
              "nothing was run (--help for usage)", file=sys.stderr)
        return 2
    scripts = [p for p in every if not names or any(n in p.name for n in names)]
    enc = child_encoding()
    bad = 0
    for p in scripts:
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, str(p)], cwd=HERE, capture_output=True, text=True,
                               encoding=enc, errors="replace", timeout=LIMIT_S)
            out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
        except subprocess.TimeoutExpired as e:
            # A hung script printed a bare FAIL that did not say it had timed out, and dropped its
            # stderr, where a traceback or a stuck child's complaint lands (review, 2026-10-02).
            # POSIX hands back the partial output as bytes even in text mode; Windows as str.
            partial = "".join(s.decode(enc, "replace") if isinstance(s, bytes) else (s or "")
                              for s in (e.stdout, e.stderr))
            out = "\n".join(t for t in (partial.rstrip("\n"), f"TIMEOUT after {LIMIT_S}s") if t)
            rc = "TIMEOUT"
        last = next((l for l in reversed(out.splitlines()) if l.strip()), "")
        print(f"{'ok  ' if rc == 0 else 'FAIL'} {p.name:<42} {time.monotonic() - t0:6.1f}s  {last[:90]}")
        if rc != 0:
            bad += 1
            print("\n".join("     | " + l for l in out.splitlines()[-40:]))
    print(f"\n{len(scripts) - bad}/{len(scripts)} scripts passed")
    # 1, not the count: two failed scripts exited 2, the code a usage error exits with.
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
