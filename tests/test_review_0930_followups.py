"""Regressions for the cross-file follow-ups of the 2026-09-30 review, one check per change.

1. sync.py reads config.problem() itself: an unreadable palimpsest.json is logged before the first
   step and counted as a `config` failure, so the run's own summary is not "all steps clean".
2. The session opener says near the top that palimpsest.json is unreadable and it runs on defaults.
3. vault_push says palimpsest.json is unreadable instead of "`push_remote` is not set".
4. state.py reads entities.json, facts.jsonl and proposed.jsonl through a UTF-8 BOM (a crash, or
   a silently dropped first fact, before), and replaces entities.json atomically.
5. ask.py puts a STATE block in the prompt when the ledger cannot be read, so the answer is
   flagged unverified instead of reading as if there were no ledger.
6. ask.py's STATE block flags a current fact whose valid_from is still in the future.
7. weekly_review stamps its generated block with the date only, so two machines render it alike.
8. The opener takes the briefing from a well-formed marker pair, not from an orphaned start marker.
9. tests/_util.py removes the temp vaults and dirs it made when the test script exits.

Checks 1-8 fail with PALIMPSEST_TOOLS pointed at tools/ from 5209d6e. Check 9 tests tests/_util.py
itself, which PALIMPSEST_TOOLS does not swap; it failed before the atexit cleanup was added.
"""
import json, os, stat, subprocess, sys, textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import _util
from _util import Checks, REPO, git, make_vault, run, stub, stub_path, write

BAD_CFG = '{"steps": {"pull": true, "push": true},\n "push_remote": "backup",}\n'
STEP_SCRIPTS = ("import_claude.py", "extract_notes.py", "link_notes.py", "maintenance.py", "dedupe.py",
                "triage_skills.py", "weekly_review.py", "embed.py", "briefing.py")
CLAUDE_STUB = ("import os, sys\n"
               "open(os.environ['STUB_PROMPT_OUT'], 'w', encoding='utf-8').write(sys.stdin.read())\n"
               "print('stub answer')\n")


def opener(v: Path) -> str:
    env = {k: val for k, val in os.environ.items() if k != "CLAUDE_BRAIN_NO_HOOK"}
    r = subprocess.run([sys.executable, str(v / "tools" / "hook_session_start.py")], cwd=v,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    try:
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return f"<no opener: exit {r.returncode}> {(r.stdout + r.stderr)[-300:]}"


def stub_claude(v: Path) -> dict:
    stub(v / ".stubs" / "bin", "claude", CLAUDE_STUB)
    return {"PATH": stub_path(v / ".stubs" / "bin"),
            "STUB_PROMPT_OUT": str(v / ".stubs" / "prompt.txt"), "CLAUDE_BRAIN_NO_HOOK": "1",
            "PALIMPSEST_MACHINE": "alpha"}


def st(v: Path, *args: str) -> subprocess.CompletedProcess:
    return run(v, "state.py", *args, env={"PALIMPSEST_MACHINE": "alpha"})


def check_sync_config(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", BAD_CFG)
    for s in STEP_SCRIPTS:
        write(v, f"tools/{s}", "print('stub')\n")
    r = subprocess.run([sys.executable, str(v / "tools" / "sync.py")], cwd=v, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    log = (v / "tools" / "sync.log").read_text(encoding="utf-8") if (v / "tools" / "sync.log").exists() else ""
    first_step = log.find("] === ")      # the first step header
    at = log.find("!! config: ")
    receipt = {}
    try:
        receipt = json.loads((v / "tools" / ".sync_status.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    c.ok(0 <= at < first_step and "palimpsest.json" in log[at:first_step]
         and "all steps clean" not in r.stdout and "config" in receipt.get("failures", [])
         and receipt.get("ok") is False and r.returncode == 1,
         "sync.py logs '!! config:' before the first step and counts it as a failure of the run",
         f"exit={r.returncode} receipt={receipt}\nlog={log[:400]!r}\nstdout={r.stdout[-300:]!r}")


def check_opener_config(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", BAD_CFG)
    for s in ("briefing.py", "maintenance.py"):
        write(v, f"tools/{s}", "print('stub')\n")
    ctx = opener(v)
    warn = ctx.find("**Config:**")
    sync = ctx.find("**Sync:**")
    c.ok(0 <= warn < max(sync, 0) and "palimpsest.json" in ctx[warn:sync] and "defaults" in ctx[warn:sync]
         and "OFF" in ctx[warn:sync],
         "the opener warns near the top that palimpsest.json is unreadable and it runs on defaults", ctx[:500])


def check_push_config(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", BAD_CFG)
    r = run(v, "vault_push.py", "--pull-only")
    out = r.stdout      # stderr carries config.load()'s own warning either way
    c.ok("palimpsest.json is unreadable" in out and "is not set" not in out,
         "vault_push says palimpsest.json is unreadable, not that push_remote is unset", out[-400:])


def check_state_bom(c: Checks) -> None:
    v = make_vault()
    st(v, "register", "my-api", "--kind", "service")
    st(v, "add", "my-api", "host", "server-1")
    ent = v / "State" / "entities.json"
    facts = v / "State" / "facts.jsonl"
    ent.write_bytes(b"\xef\xbb\xbf" + ent.read_bytes())
    r = st(v, "show", "my-api")
    c.ok(r.returncode == 0 and "server-1" in r.stdout,
         "an entities.json saved with a BOM still loads", (r.stdout + r.stderr)[-300:])
    ent.write_bytes(ent.read_bytes().lstrip(b"\xef\xbb\xbf"))
    facts.write_bytes(b"\xef\xbb\xbf" + facts.read_bytes())
    r = st(v, "show", "my-api")
    c.ok(r.returncode == 0 and "server-1" in r.stdout,
         "the first fact of a facts.jsonl saved with a BOM is not dropped", (r.stdout + r.stderr)[-300:])
    prop = {"t": "2026-09-30T10:00:00Z", "valid_from": "2026-09-30T10:00:00Z", "entity": "my-api",
            "attr": "port", "value": "8080", "kind": "asserted", "by": "test", "source": [],
            "id": "abcdef012345", "note": ""}
    (v / "State" / "proposed.jsonl").write_bytes(b"\xef\xbb\xbf" + (json.dumps(prop) + "\n").encode())
    r = st(v, "accept", "--all")
    c.ok(r.returncode == 0 and "accepted 1" in r.stdout,
         "the first line of a proposed.jsonl saved with a BOM is accepted", (r.stdout + r.stderr)[-300:])
    before = ent.stat().st_ino
    r = st(v, "register", "my-api", "--hot")
    leftovers = [p.name for d in (v / "State", v / "tools" / "logs") if d.is_dir() for p in d.iterdir()
                 if p.name.startswith(("entities", ".entities")) and p.name != "entities.json"]
    c.ok(r.returncode == 0 and ent.stat().st_ino != before and not leftovers
         and json.loads(ent.read_text(encoding="utf-8"))["entities"]["my-api"].get("hot") is True,
         "entities.json is replaced atomically (a new file renamed over it, no temp file left)",
         f"exit={r.returncode} same inode={ent.stat().st_ino == before} leftovers={leftovers}")


def check_ask_state_unreadable(c: Checks) -> None:
    v = make_vault()
    write(v, "10 Notes/Deploy notes.md", "The api deploy runs on the build box.\n")
    write(v, "State/entities.json", '{"entities": {"my-api": {"kind": "service"},}\n')
    env = stub_claude(v)
    r = run(v, "ask.py", "--mode", "lexical", "--no-rerank", "where does the api deploy run", env=env)
    prompt = Path(env["STUB_PROMPT_OUT"])
    text = prompt.read_text(encoding="utf-8") if prompt.exists() else ""
    block = text[text.find("### STATE"):] if "### STATE" in text else ""
    c.ok(r.returncode == 0 and "could not be read" in block and "unverified" in block,
         "an unreadable ledger puts a STATE block in the prompt saying state answers are unverified",
         f"exit={r.returncode}\n{text[:600]!r}\n{r.stderr[-300:]}")


def check_ask_future(c: Checks) -> None:
    v = make_vault()
    st(v, "register", "my-api", "--kind", "service")
    ahead = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r0 = st(v, "add", "my-api", "host", "server-2", "--since", ahead, "--future")
    r = run(v, "ask.py", "--mode", "lexical", "--no-rerank", "--retrieve-only", "where does my-api run",
            env=stub_claude(v))
    line = next((l for l in r.stdout.splitlines() if "my-api.host" in l), "")
    c.ok(r0.returncode == 0 and "server-2" in line and "FUTURE-DATED" in line and ahead in line,
         "ask's STATE block flags a current fact that is not valid until a future time",
         f"add: {(r0.stdout + r0.stderr)[-200:]}\n{r.stdout[-500:]}{r.stderr[-200:]}")


def check_weekly_stamp(c: Checks) -> None:
    v = make_vault()
    write(v, "10 Notes/a.md", "---\ncreated: 2026-09-30\n---\na\n")
    r = run(v, "weekly_review.py")
    out = next(iter(sorted((v / "Reviews" / "Weekly").glob("*.md"))), None) if (v / "Reviews" / "Weekly").exists() else None
    text = out.read_text(encoding="utf-8") if out else ""
    stamp = next((l for l in text.splitlines() if l.startswith("*Generated ")), "")
    c.ok(r.returncode == 0 and stamp.startswith(f"*Generated {date.today():%Y-%m-%d}.")
         and not any(ch.isdigit() for ch in stamp.split(".")[0][len("*Generated 2026-09-30"):]),
         "the weekly review's generated stamp carries the date only, no minute", repr(stamp) + r.stderr[-200:])


def check_opener_orphan_marker(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", json.dumps({"version": 1}))
    for s in ("briefing.py", "maintenance.py"):
        write(v, f"tools/{s}", "print('stub')\n")
    write(v, f"Daily/{date.today().isoformat()}.md",
          "# Today\n<!-- briefing:start -->\nSTALE ORPHANED BRIEFING TEXT\n\n## Notes\nmine\n"
          "<!-- briefing:start -->\n## Fresh briefing\n- one\n<!-- briefing:end -->\n")
    ctx = opener(v)
    c.ok("Fresh briefing" in ctx and "STALE ORPHANED" not in ctx and "## Notes" not in ctx,
         "the opener takes the well-formed briefing block, not the text after an orphaned start marker", ctx[-500:])


def check_util_cleanup(c: Checks) -> None:
    code = textwrap.dedent("""
        import sys; sys.path.insert(0, sys.argv[1])
        import _util
        v = _util.make_vault()
        print(v)
    """)
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + code, str(REPO / "tests")], capture_output=True, text=True, encoding="utf-8", errors="replace")
    made = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    c.ok(made is not None and not made.exists(),
         "a vault made by _util.make_vault is removed when the test script exits", f"{made} {r.stderr[-300:]}")


def main() -> int:
    c = Checks("review 2026-09-30 — cross-file follow-ups")
    for check in (check_sync_config, check_opener_config, check_push_config, check_state_bom,
                  check_ask_state_unreadable, check_ask_future, check_weekly_stamp,
                  check_opener_orphan_marker, check_util_cleanup):
        try:
            check(c)
        except Exception as e:
            c.ok(False, f"{check.__name__} raised", f"{type(e).__name__}: {e}")
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
