"""Regressions for the fifth external review (2026-09-30).

1. [P1] Redaction removes the WHOLE private-key block (it replaced only the BEGIN line, leaving
   the base64 body, which a restored header turned back into a working key) — for a key pasted
   plainly, JSON-escaped in tool output, or cut off before END.
2. An extraction item missing a required field fails the conversation instead of being dropped
   while the conversation is checkpointed as done.
3. Refreshing the briefing keeps a Windows path literal (the block was used as a regex
   replacement string, so "C:\\Users" raised "bad escape \\U").
4. scan_secrets.py accepts a relative directory argument (it crashed in relative_to()).
"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
from _util import Checks, make_vault, run, write


def load(v, name):
    sys.path.insert(0, str(v / "tools"))
    sys.modules.pop(name, None)
    return __import__(name)


def main() -> int:
    c = Checks("review findings 2026-09-30 (d)")
    v = make_vault()

    # 1. whole-key redaction, on a key generated here and thrown away
    redact = load(v, "redact")
    if shutil.which("ssh-keygen"):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "k")], check=True)
        key = (d / "k").read_text()
        shutil.rmtree(d)
        body = [l for l in key.splitlines() if l and not l.startswith("-----") and len(l) >= 16]
        for name, text in (("plain", f"my key:\n{key}\nafter"), ("json", json.dumps({"out": key})),
                           ("truncated", key[: len(key) // 2])):
            out, _ = redact.redact_text(text)
            c.ok(not any(l in out for l in body) and "[redacted]" in out,
                 f"redaction removes the whole key body ({name})", out[:160])
    else:
        print("SKIP: ssh-keygen not available")

    # 2. an item missing a required field is a failure
    en = load(v, "extract_notes")
    try:
        en.parse_notes('[{"title": "An insight"}]')
        c.ok(False, "parse_notes rejects an item without a body")
    except ValueError as e:
        c.ok("body" in str(e), "parse_notes rejects an item without a body", str(e))
    es = load(v, "extract_skills")
    try:
        es.parse_skills('[{"name": "A skill"}]')
        c.ok(False, "parse_skills rejects an item without steps")
    except ValueError as e:
        c.ok("steps" in str(e), "parse_skills rejects an item without steps", str(e))

    # 3. briefing refresh with a Windows path in the block
    br = load(v, "briefing")
    br.DAILY = v / "Daily"
    br.build_block = lambda: f"{br.START}\n- [ ] tidy C:\\Users\\alice\\notes\n{br.END}"
    ok = True
    try:
        br.main(); br.main()                       # create, then refresh in place
    except Exception as e:
        ok = False; err = f"{type(e).__name__}: {e}"
    daily = next((v / "Daily").glob("*.md"), None)
    c.ok(ok and daily and "C:\\Users\\alice\\notes" in daily.read_text(),
         "briefing refresh keeps a Windows path literal", "" if ok else err)

    # 4. relative directory argument
    write(v, "10 Notes/x.md", "nothing secret\n")
    r = run(v, "scan_secrets.py", "10 Notes")
    c.ok(r.returncode == 0 and "Traceback" not in r.stderr, "scan_secrets.py accepts a relative directory",
         (r.stdout + r.stderr)[-300:])
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
