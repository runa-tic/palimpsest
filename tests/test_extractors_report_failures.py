"""An extraction run where conversations failed must not report success.

Codex review P2: extract_notes.py / extract_skills.py caught each conversation's failure,
printed "skipped" and carried on — then exited 0 even when EVERY conversation failed, so
sync.py recorded the step as clean. They now keep going, but exit non-zero when any
conversation failed, and sync.py marks the step failed.

A fake `claude` on PATH fails any conversation containing FAILME and returns [] otherwise.
"""
import json, sys
from _util import Checks, make_vault, run, stub, stub_path, write

FAKE_CLAUDE = """import sys
if "FAILME" in sys.stdin.read():
    print("usage limit reached")
    sys.exit(1)
print("[]")
"""


def main() -> int:
    c = Checks("extractors: failures are reported")
    v = make_vault()
    bindir = v / "fakebin"
    stub(bindir, "claude", FAKE_CLAUDE)
    env = {"PATH": stub_path(bindir)}
    conv = "40 Resources/Claude Conversations/Claude Code/demo"
    write(v, f"{conv}/2026-09-01 good (aaaaaaaa).md", "---\ntype: x\n---\n\n# good\n\nfine talk\n")
    write(v, f"{conv}/2026-09-01 bad (bbbbbbbb).md", "---\ntype: x\n---\n\n# bad\n\nFAILME please\n")

    for script, state in (("extract_notes.py", ".extract_state.json"),
                          ("extract_skills.py", ".extract_skills_state.json")):
        r = run(v, script, env=env)
        c.ok(r.returncode != 0, f"{script}: exits non-zero when a conversation failed", r.stdout[-400:])
        c.ok("good" in r.stdout and "bad" in r.stdout, f"{script}: still processes the others", r.stdout[-400:])
        st = json.loads((v / "tools" / state).read_text()) if (v / "tools" / state).exists() else {}
        keys = " ".join(st)
        c.ok("good" in keys and "bad" not in keys, f"{script}: the failure is not recorded as done (retries next run)",
             keys)

    v2 = make_vault()
    stub(v2 / "fakebin", "claude", FAKE_CLAUDE)
    env2 = {"PATH": stub_path(v2 / "fakebin")}
    write(v2, f"{conv}/2026-09-01 bad (bbbbbbbb).md", "---\ntype: x\n---\n\n# bad\n\nFAILME\n")
    steps = {k: False for k in ("import", "extract", "skills", "link", "maintenance", "dedupe", "triage",
                                "weekly", "embed", "briefing", "push", "pull")}
    steps["extract"] = True
    write(v2, "palimpsest.json", json.dumps({"version": 1, "steps": steps}))
    run(v2, "sync.py", env=env2)
    status = json.loads((v2 / "tools" / ".sync_status.json").read_text())
    c.ok(status.get("failures") == ["extract"], "sync.py records the extract step as FAILED", str(status))
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
