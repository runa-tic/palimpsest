"""Regressions for the review of 2026-10-06, second batch: three findings.

5. vault_push.py pulls with `git pull --rebase --autostash`, and git re-applies an autostash to the
   working tree only: an edit staged by hand came back unstaged, and where the file had been
   edited again after staging, the staged version was gone. It happened on every run that pulled
   with a local commit to carry, which is every run that had content to back up, even with the
   remote already current. What was staged is now put back after the pull, for every path the
   pull did not change; a path it did change is named, and its edits are in the working tree.
6. `state.py show` and ask.py's STATE block dropped load_facts' count of lines that did not parse.
   With the newest fact of an attribute cut short (a kill mid-append, a bad merge), the fold fell
   back to the fact before it and both presented that as the current value, with nothing said.
   Every reader of the fold now says how many ledger lines could not be read: `show` (text, JSON
   and the opener), `station`, the State Register, and ask.py's prompt.
7. `vault_push.py --pull-only` exited 0 when the configured push_remote did not exist (a typo, a
   remote removed), so with the pull step on, sync.py reported "all steps clean" and rendered the
   briefing on a tree that had never been pulled. A remote that is named but missing, and a
   config that cannot be read, now fail the step, so the briefing guard applies; no push_remote
   at all is still nothing to pull from, and exits 0.

With PALIMPSEST_TOOLS pointed at the tools from before this change, every check fails except those
marked "(held before)". ask.py's model call goes to a fake `claude` first on PATH.
"""
import json, os, subprocess, sys
from datetime import date
from pathlib import Path
from _util import Checks, copy_tools, git, make_vault, run, stub, stub_path, tempdir, write

RUN = "a\nb\nc\nd\ne\nf\ng\n"
WANT = ["A\tscripts/new.py", "M\tscripts/other.py", "M\tscripts/run.py"]


def clone(remote: Path, name: str) -> Path:
    d = tempdir(f"palimpsest-{name}-")
    subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)   # quiet about an empty remote
    git(d, "config", "user.name", name); git(d, "config", "user.email", f"{name}@example.invalid")
    copy_tools(d / "tools")
    write(d, "palimpsest.json", json.dumps({"version": 1, "push_remote": "origin"}))
    return d


def stage(v: Path) -> None:
    """A code edit staged and then edited again, one staged as it stands, and a new file staged."""
    write(v, "scripts/run.py", RUN.replace("a\n", "A2\n", 1)); git(v, "add", "scripts/run.py")
    write(v, "scripts/run.py", RUN.replace("a\n", "A3\n", 1))
    write(v, "scripts/other.py", "other, edited\n"); git(v, "add", "scripts/other.py")
    write(v, "scripts/new.py", "n1\n"); git(v, "add", "scripts/new.py")


def staged(v: Path) -> list[str]:
    return sorted(l for l in git(v, "diff", "--cached", "--name-status", "--no-renames").stdout.splitlines() if l)


def first_line(v: Path, where: str) -> str:
    """The first line of scripts/run.py in the index (":") or the working tree ("")."""
    text = git(v, "show", ":scripts/run.py").stdout if where == ":" else (v / "scripts" / "run.py").read_text(encoding="utf-8")
    return text.splitlines()[0] if text else ""


def check_staging(c: Checks) -> None:
    remote = tempdir("palimpsest-remote-") / "vault.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = clone(remote, "seed")
    write(seed, ".gitignore", "tools/\npalimpsest.json\n")
    write(seed, "10 Notes/shared.md", "line one\nline two\n")
    write(seed, "scripts/run.py", RUN)
    write(seed, "scripts/other.py", "other\n")
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")
    a, b1, b2, b3, b4 = (clone(remote, n) for n in ("a", "b1", "b2", "b3", "b4"))

    # The remote is current; this run has content to commit, so its pull carries a local commit.
    stage(b1)
    write(b1, "10 Notes/from-b1.md", "written on b1\n")
    r = run(b1, "vault_push.py")
    sent = git(b1, "show", "--name-only", "--format=", "HEAD").stdout.split("\n")
    c.ok(r.returncode == 0 and "pushed" in r.stdout and [s for s in sent if s] == ["10 Notes/from-b1.md"],
         "5. a backup with code staged by hand still commits and pushes the content alone (held before)",
         r.stdout + str(sent))
    c.ok(staged(b1) == WANT and first_line(b1, ":") == "A2" and first_line(b1, "") == "A3",
         "5. ...and what was staged is still staged, as it was staged, with the later edit in the tree",
         f"{staged(b1)} index={first_line(b1, ':')!r} tree={first_line(b1, '')!r}")

    # The remote is ahead (b1's note): --pull-only fast-forwards.
    stage(b2)
    r = run(b2, "vault_push.py", "--pull-only")
    c.ok(r.returncode == 0 and (b2 / "10 Notes" / "from-b1.md").exists()
         and staged(b2) == WANT and first_line(b2, ":") == "A2" and first_line(b2, "") == "A3",
         "5. --pull-only brings the other machine's note and keeps the staging too",
         f"rc={r.returncode} {staged(b2)} index={first_line(b2, ':')!r} {r.stdout[-200:]}")

    # The other machine changed a staged file: that one cannot be put back as it was staged.
    git(a, "pull", "-q", "origin", "main")
    write(a, "scripts/run.py", RUN.replace("g\n", "G-upstream\n", 1))
    git(a, "add", "scripts/run.py"); git(a, "commit", "-q", "-m", "run.py, on a"); git(a, "push", "-q", "origin", "main")
    stage(b3)
    r = run(b3, "vault_push.py", "--pull-only")
    tree = (b3 / "scripts" / "run.py").read_text(encoding="utf-8")
    lines = r.stdout.strip().splitlines()
    c.ok(r.returncode == 0 and "A3" in tree and "G-upstream" in tree
         and staged(b3) == ["A\tscripts/new.py", "M\tscripts/other.py"],
         "5. a staged file the pull changed keeps both edits in the tree, and the other staging is kept",
         f"rc={r.returncode} {staged(b3)} {tree!r} {r.stdout[-300:]}")
    c.ok(any("scripts/run.py" in l and "no longer staged" in l for l in lines)
         and bool(lines) and lines[-1].startswith("vault-push: pull: rebased"),
         "5. ...and the run names the file it could not re-stage, before the line that reports the pull",
         r.stdout[-400:])

    # A conflict: the rebase is aborted, and the abort re-applies the autostash the same way.
    write(a, "10 Notes/shared.md", "line one EDITED ON A\nline two\n")
    run(a, "vault_push.py")
    stage(b4)
    write(b4, "10 Notes/shared.md", "line one EDITED ON B4\nline two\n")
    r = run(b4, "vault_push.py")
    c.ok(r.returncode == 1 and "PULL CONFLICT" in r.stdout
         and staged(b4) == WANT and first_line(b4, ":") == "A2" and first_line(b4, "") == "A3",
         "5. a pull that stops on a conflict is aborted with the staging as it was",
         f"rc={r.returncode} {staged(b4)} index={first_line(b4, ':')!r} {r.stdout[-300:]}")


PROMPT_STUB = ("import os, sys\n"
               "prompt = sys.stdin.buffer.read().decode('utf-8', 'replace')\n"
               "with open(os.environ['STUB_PROMPT'], 'w', encoding='utf-8') as fh:\n"
               "    fh.write(prompt)\n"
               "print('STUB-ANSWER')\n")
CFG = json.dumps({"version": 1, "steps": {"pull": False}})


def ledger_views(v: Path, env: dict) -> dict[str, str]:
    """Everything that presents the fold as the current state."""
    st = lambda *a: run(v, "state.py", *a)
    out = {"show": st("show", "svc").stdout, "opener": st("show", "--hot", "--opener").stdout,
           "json": st("show", "svc", "--json").stdout, "station": st("station", "show").stdout}
    st("fold")
    reg = next(iter(v.rglob("*Register.md")), None)
    out["register"] = reg.read_text(encoding="utf-8") if reg else ""
    prompt = v / "prompt.txt"
    if prompt.exists():
        prompt.unlink()
    r = run(v, "ask.py", "--mode", "lexical", "where does svc run", env=env)
    out["ask"] = prompt.read_text(encoding="utf-8") if prompt.exists() else f"(no prompt) {r.stdout[-200:]} {r.stderr[-200:]}"
    return out


def check_ledger(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    write(v, "10 Notes/svc.md", "# svc\n\nsvc is the service; where svc runs is in the ledger\n")
    stub(v / "fakebin", "claude", PROMPT_STUB)
    env = {"PATH": stub_path(v / "fakebin"), "STUB_PROMPT": str(v / "prompt.txt")}
    run(v, "state.py", "register", "svc", "--kind", "service", "--hot")
    run(v, "state.py", "add", "svc", "host", "box", "--since", "2026-09-01T00:00:00Z")
    run(v, "state.py", "add", "svc", "host", "vps")
    whole = ledger_views(v, env)
    said = lambda text: "could not be read" in text and "1 ledger line" in text
    c.ok(all("vps" in t for k, t in whole.items() if k != "station") and not any(said(t) for t in whole.values())
         and json.loads(whole["json"]).get("malformed") in (None, 0),
         "6. an intact ledger gives the newest host everywhere, with no warning (held before)",
         str({k: t[-200:] for k, t in whole.items()}))

    facts = v / "State" / "facts.jsonl"
    lines = facts.read_bytes().splitlines()
    facts.write_bytes(b"\n".join(lines[:-1]) + b"\n" + lines[-1][: len(lines[-1]) // 2])   # the newest fact, cut short
    cut = ledger_views(v, env)
    for name, where in (("show", "`state.py show`"), ("opener", "the session opener"),
                        ("register", "the State Register"), ("ask", "ask.py's prompt")):
        c.ok("box" in cut[name] and said(cut[name]),
             f"6. with the newest host fact cut short, {where} gives the older host and says a ledger line is unreadable",
             cut[name][-400:])
    c.ok(said(cut["station"]) and "station lease:" in cut["station"],
         "6. ...as does `state.py station show`, whose holder comes from the fold too", cut["station"][-300:])
    try:
        n = json.loads(cut["json"]).get("malformed")
    except ValueError:
        n = None
    c.ok(n == 1, "6. ...and `show --json` carries the count", cut["json"][-300:])


def check_pull_config(c: Checks) -> None:
    v = make_vault()
    write(v, ".gitignore", "__pycache__/\n")
    write(v, "10 Notes/x.md", "x\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    steps = {k: False for k in ("import", "extract", "skills", "link", "maintenance", "dedupe", "triage",
                                "weekly", "embed", "state", "push")}
    steps.update({"pull": True, "briefing": True})
    daily = v / "Daily" / f"{date.today().isoformat()}.md"

    write(v, "palimpsest.json", json.dumps({"version": 1, "push_remote": "backup", "steps": steps}))   # no such remote
    r = run(v, "vault_push.py", "--pull-only")
    c.ok(r.returncode == 1 and "remote 'backup' does not exist" in r.stdout,
         "7. --pull-only fails when the configured push_remote does not exist", f"rc={r.returncode} {r.stdout}")
    r = run(v, "sync.py")
    status = json.loads((v / "tools" / ".sync_status.json").read_text(encoding="utf-8"))
    c.ok("briefing: SKIPPED" in r.stdout and status.get("failures") == ["pull", "briefing"]
         and status.get("ok") is False and not daily.exists(),
         "7. ...so the sync records the pull as failed and does not render the briefing",
         f"{status} {r.stdout[-300:]}")
    r = run(v, "vault_push.py", "--pull-only", "--dry-run")
    c.ok(r.returncode == 1, "7. ...and a dry run fails the same way", f"rc={r.returncode} {r.stdout}")

    write(v, "palimpsest.json", "{ this is not JSON")
    r = run(v, "vault_push.py", "--pull-only")
    c.ok(r.returncode == 1 and "push_remote cannot be read" in r.stdout,
         "7. --pull-only fails when palimpsest.json cannot be read", f"rc={r.returncode} {r.stdout}")

    write(v, "palimpsest.json", json.dumps({"version": 1, "steps": steps}))                              # none named
    r = run(v, "vault_push.py", "--pull-only")
    c.ok(r.returncode == 0 and "push_remote" in r.stdout and "is not set" in r.stdout,
         "7. with no push_remote at all there is nothing to pull from, and that is not a failure (held before)",
         f"rc={r.returncode} {r.stdout}")


def main() -> int:
    c = Checks("review 2026-10-06 (b): staging across a pull, a damaged ledger, a missing remote")
    check_staging(c)
    check_ledger(c)
    check_pull_config(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
