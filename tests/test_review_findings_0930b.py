"""Regressions for the third external review (2026-09-30, on the published main), P2 findings.

1. A malformed or truncated extraction reply is a failure, not an empty answer: it used to parse
   as [] and the conversation was checkpointed as done, so it never retried.
2. A session keeps one conversation note when its title changes between imports (the first
   message, later an AI title); a fresh title-derived filename used to leave both.
3. Deleting a whole tracked content directory is backed up: only directories still on disk
   were staged, so the run said "nothing new to commit" and the deletions never left the machine.
"""
import json, os, subprocess, sys
from pathlib import Path
from _util import Checks, git, make_vault, rmtree, run, tempdir, write


def load(v, name):
    sys.path.insert(0, str(v / "tools"))
    sys.modules.pop(name, None)
    return __import__(name)


def main() -> int:
    c = Checks("review findings 2026-09-30 (b)")

    # 1. parse failures raise, genuine [] does not; a failing conversation is not checkpointed
    v = make_vault()
    en, es = load(v, "extract_notes"), load(v, "extract_skills")
    for mod, fn, item in ((en, "parse_notes", {"title": "t", "body": "b"}),
                          (es, "parse_skills", {"name": "n", "steps": "s"})):
        parse = getattr(mod, fn)
        c.ok(parse("[]") == [] and parse(f"```json\n[{json.dumps(item)}]\n```") == [item],
             f"{fn}: a genuine [] and a fenced array still parse")
        bad = 0
        for raw in ("", "no json here", '[{"title": "cut off', '{"title": "not a list"}'):
            try:
                parse(raw)
            except ValueError:
                bad += 1
        c.ok(bad == 4, f"{fn}: empty / prose / truncated / non-array replies raise", f"{bad}/4 raised")
    conv = v / "40 Resources" / "Claude Conversations" / "Claude Code" / "p"
    write(v, str((conv / "2026-09-30 t (abcd1234).md").relative_to(v)), "---\nx: 1\n---\nhello\n")
    saved = {}
    en.CONV_DIR, en.VAULT = conv.parent.parent, v
    en.load_state = lambda: {}
    en.save_state = lambda s: saved.update(s)
    en._claude = lambda *a, **k: '[{"title": "trunc'          # the model reply that used to "succeed"
    en.call_claude = en._claude
    sys.argv = ["extract_notes.py"]
    try:
        rc = en.main()
    except SystemExit as e:                                   # the port's main() exits itself
        rc = e.code
    c.ok(rc == 1 and not saved, "a malformed reply fails the run and is NOT checkpointed", f"rc={rc} saved={list(saved)}")

    # 2. one note per session across a title change
    v = make_vault()
    ic = load(v, "import_claude")
    ic.OUT_BASE = v / "40 Resources" / "Claude Conversations"
    proj = v / "projects" / "-home-me-proj"; proj.mkdir(parents=True)
    tr = proj / "abcd1234-0000-0000-0000-000000000000.jsonl"
    user = {"type": "user", "timestamp": "2026-09-30T01:00:00Z", "message": {"role": "user", "content": "first message words"}}
    tr.write_text(json.dumps(user) + "\n")
    p1 = ic.process_transcript(tr)
    with tr.open("a") as fh:
        fh.write(json.dumps({"type": "summary", "summary": "A proper AI title"}) + "\n")
        fh.write(json.dumps({"type": "ai-title", "aiTitle": "A proper AI title"}) + "\n")
    p2 = ic.process_transcript(tr)
    notes = sorted((ic.OUT_BASE / "Claude Code").rglob("*(abcd1234).md"))
    c.ok(p1 is not None and len(notes) == 1, "a title change keeps ONE note for the session",
         f"{[n.name for n in notes]}")

    # 3. a deleted content directory's deletions are committed
    v = make_vault()
    remote = tempdir("palimpsest-remote-") / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    write(v, "10 Notes/a.md", "a\n"); write(v, "Daily/d.md", "d\n")
    write(v, ".gitignore", "tools/__pycache__/\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    git(v, "remote", "add", "origin", str(remote)); git(v, "push", "-q", "-u", "origin", "main")
    rmtree(v / "10 Notes", ignore_errors=False)
    r = run(v, "vault_push.py")
    tracked = git(v, "ls-files", "--", "10 Notes").stdout.strip()
    c.ok(tracked == "" and "nothing new to commit" not in r.stdout, "deleting a whole content dir is committed",
         (r.stdout + r.stderr)[-400:] + f" | still tracked: {tracked!r}")
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
