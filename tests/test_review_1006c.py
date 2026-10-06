"""Regressions for a review of 2026-10-06, third batch: two small findings in tools that run at
every session start.

8. `state.py probe --only <name>` with a name that is no probe printed "no such probe", then
   listed the name under "probes run" and exited 0: read by its last line or its exit code, a
   mistyped name was a clean probe. The summary now lists only the probes that exist, and the
   command exits 2, as every other refusal in state.py does. Nothing is appended either way.
9. maintenance.py read every entry of Claude's memory directory with nothing catching a failure
   to open one. An entry that cannot be read (a directory named like a note, a file gone since
   the listing, a dangling link) ended the whole health pass with a traceback, and the session
   opener, which reads only the pass's output, lost its health line with nothing said. Such an
   entry is skipped.

With PALIMPSEST_TOOLS pointed at the tools from before this change, every check fails except the
one marked "(held before)".
"""
import json, os, re, sys, time
from _util import Checks, make_vault, run, tempdir, write

CFG = json.dumps({"version": 1, "steps": {"pull": False}})


def check_probe(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    r = run(v, "state.py", "probe", "--only", "nosuch,")
    facts = (v / "State" / "facts.jsonl").read_text(encoding="utf-8") if (v / "State" / "facts.jsonl").exists() else ""
    c.ok("nosuch: no such probe" in r.stdout and "(empty): no such probe" in r.stdout and "nosuch" not in facts,
         "8. an unknown probe name, and an empty one, are refused by name and nothing is appended",
         f"rc={r.returncode} {r.stdout[-300:]} {facts[-200:]!r}")
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    c.ok(r.returncode == 2 and last.endswith("probes run: none"),
         "8. ...and the run exits 2 and does not list the name as a probe that ran", f"rc={r.returncode} {last!r}")
    r = run(v, "state.py", "probe", "--only", "sync,nosuch")
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    c.ok(r.returncode == 2 and last.endswith("probes run: sync"),
         "8. with a real probe beside it, that one runs and is the only one listed; the exit is still 2",
         f"rc={r.returncode} {r.stdout[-300:]}")
    r = run(v, "state.py", "probe", "--only", "sync")
    c.ok(r.returncode == 0 and r.stdout.strip().splitlines()[-1].endswith("probes run: sync"),
         "8. a run of known probes only still exits 0 (held before)", f"rc={r.returncode} {r.stdout[-200:]}")


def check_memory(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    write(v, "10 Notes/n.md", "---\ntype: note\ntags:\n  - x\n---\n\n# n\n\na note\n")
    mem = tempdir("palimpsest-memory-")
    (mem / "not-a-file.md").mkdir()                                   # matches *.md, cannot be read
    good = mem / "service-deployed.md"
    good.write_text("---\nname: service-deployed\ndescription: the service is DEPLOYED and live\n---\n\nIt is running.\n",
                    encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(good, (old, old))
    r = run(v, "maintenance.py", env={"PALIMPSEST_MEMORY_DIR": str(mem)})
    out = r.stdout + r.stderr
    m = re.search(r"memstale=(\d+)", out)
    c.ok(r.returncode == 0 and "Traceback" not in out and bool(m) and int(m.group(1)) == 1,
         "9. a memory entry that cannot be opened is skipped: the pass finishes and still counts the stale entry beside it",
         f"rc={r.returncode} {out[-400:]}")
    c.ok((v / "Reviews" / "Vault Health.md").exists(), "9. ...and the health report is written", out[-200:])


def main() -> int:
    c = Checks("review 2026-10-06 (c): refused probe names, unreadable memory entries")
    check_probe(c)
    check_memory(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
