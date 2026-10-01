"""Never render today's briefing on a tree that may be behind the other machine.

In the source vault, a machine whose pull had failed created the day's daily note from scratch;
the push-time rebase then union-merged it with the other machine's copy, and because the two
renders listed projects in different (filesystem) orders, union could not collapse them: the
day carried two briefing blocks. Guard: pull first; if it fails, skip the briefing (sync) or
leave the note alone and say so (session hook). And sort the lists so renders are identical.
"""
import json, subprocess, sys
from datetime import date
from _util import Checks, make_vault, git, run, write


def main() -> int:
    c = Checks("briefing guard")
    v = make_vault()
    write(v, ".gitignore", "__pycache__/\n")
    write(v, "10 Notes/x.md", "x\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    git(v, "remote", "add", "origin", str(v / "no-such-remote.git"))   # every pull fails
    steps = {k: False for k in ("import", "extract", "skills", "link", "maintenance", "dedupe", "triage",
                                "weekly", "embed", "state", "push")}
    steps.update({"pull": True, "briefing": True})
    write(v, "palimpsest.json", json.dumps({"version": 1, "push_remote": "origin", "steps": steps}))
    daily = v / "Daily" / f"{date.today().isoformat()}.md"

    r = run(v, "sync.py")
    status = json.loads((v / "tools" / ".sync_status.json").read_text())
    c.ok("briefing: SKIPPED" in r.stdout, "sync skips the briefing when the pull failed", r.stdout)
    c.ok(status["failures"] == ["pull", "briefing"], "...and records both as failed", str(status))
    c.ok(not daily.exists(), "no daily note was created on the stale tree")

    r = subprocess.run([sys.executable, str(v / "tools" / "hook_session_start.py")], cwd=v,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""
    c.ok("briefing not refreshed" in ctx and "⚠️" in ctx, "the session opener says the briefing was not refreshed", ctx[-400:])
    c.ok(not daily.exists(), "the hook did not render on the stale tree either")

    git(v, "remote", "remove", "origin")
    write(v, "palimpsest.json", json.dumps({"version": 1, "steps": {**steps, "pull": False}}))
    for name in ("Zeta", "alpha", "Mid"):
        write(v, f"20 Projects/{name}.md", "---\nstatus: active\n---\n")
    run(v, "briefing.py")
    text = daily.read_text() if daily.exists() else ""
    order = [n for n in ("Mid", "Zeta", "alpha") if f"[[{n}]]" in text]
    pos = [text.find(f"[[{n}]]") for n in ("Mid", "Zeta", "alpha")]
    c.ok(len(order) == 3 and pos == sorted(pos), "active projects render in sorted (filesystem-independent) order",
         text[:600])
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
