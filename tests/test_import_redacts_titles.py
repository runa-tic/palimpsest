"""A conversation's title must be redacted before it becomes a filename or a header.

Codex review P1: import_claude.py redacted the note BODY in write_note(), but built the
filename from the raw title — the session's ai-title, else the first 60 chars of the first
user message — so a deny-listed term or a pasted key in that first message landed in the
file NAME, where no redaction and (until the path scan) no commit guard ever looked.
"""
import json, sys
from _util import Checks, make_vault, run, write

TERM = "zzqprivateterm"
FAKE_KEY = "AKIA" + "QZXW" * 4


def main() -> int:
    c = Checks("import_claude: redacted titles")
    v = make_vault()
    write(v, "tools/.redact_terms.txt", TERM + "\n")
    rows = [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z", "version": "x",
         "message": {"role": "user", "content": f"call {TERM} about key {FAKE_KEY} tomorrow"}},
        {"type": "assistant", "timestamp": "2026-09-01T10:00:05Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "noted"}]}},
    ]
    tr = write(v, "fixture/proj-demo/abcdef12-0000.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n")
    r = run(v, "import_claude.py", "file", str(tr))
    notes = list((v / "40 Resources" / "Claude Conversations").rglob("*.md"))
    c.ok(len(notes) == 1, "one note written", r.stdout + r.stderr)
    if notes:
        n = notes[0]
        c.ok(TERM not in n.name and FAKE_KEY not in n.name, "filename carries neither the term nor the key", n.name)
        text = n.read_text(encoding="utf-8")
        c.ok(TERM not in text and FAKE_KEY not in text, "body is redacted (as before)")
        header = next((l for l in text.splitlines() if l.startswith("# ")), "")
        c.ok(n.stem.split(" ", 1)[1].rsplit(" (", 1)[0] == header[2:].strip()[:80].rstrip()
             or header[2:].strip().startswith(n.stem.split(" ", 1)[1].rsplit(" (", 1)[0][:40]),
             "filename and # header agree", f"{n.name!r} vs {header!r}")

    v2 = make_vault()
    pad = "x" * 50                                        # the key starts at char 51 and crosses 60
    rows2 = [{"type": "user", "timestamp": "2026-09-03T10:00:00Z",
              "message": {"role": "user", "content": f"{pad} {FAKE_KEY} rest"}},
             {"type": "assistant", "timestamp": "2026-09-03T10:00:01Z",
              "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}]
    tr2 = write(v2, "fixture/proj-demo/bbbbbbbb-0000.jsonl", "\n".join(json.dumps(r) for r in rows2) + "\n")
    run(v2, "import_claude.py", "file", str(tr2))
    n2 = list((v2 / "40 Resources" / "Claude Conversations").rglob("*.md"))
    c.ok(bool(n2) and "AKIA" not in n2[0].name, "a key straddling the 60-char cut leaves no fragment in the name",
         str([x.name for x in n2]))

    export = [{"name": f"Plans with {TERM}", "created_at": "2026-09-02T09:00:00Z", "uuid": "1234abcd-x",
               "chat_messages": [{"sender": "human", "text": "hello"}, {"sender": "assistant", "text": "hi"}]}]
    ex = write(v, "fixture/conversations.json", json.dumps(export))
    run(v, "import_claude.py", "web", str(ex))
    web = list((v / "40 Resources" / "Claude Conversations" / "claude.ai").glob("*.md"))
    c.ok(len(web) == 1 and TERM not in web[0].name, "claude.ai export: filename redacted too",
         str([w.name for w in web]))
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
