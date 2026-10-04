"""Stale tasks say so (2026-10-04): the briefing's rolled-over list and the ledger's intentions.

A task lives in up to three places (a ledger fact, a line in a tracker note, the daily checkbox)
and nothing links them, so one updated alone leaves the others reading as open. The rolled-over
list is copied forward verbatim every day, and a `next` fact stays "to do" until someone appends
to the ledger. Neither is fixed here; both now say when they have gone stale.

Briefing (the rolled-over list of today's note):
1. An open task that names a day already gone, as a leading date or as "by <date>", ends in
   "⏰ *due <date> passed*"; one that has been in the daily notes for seven days or more ends in
   "⏰ *open since <date>*"; a task naming a future day, or one written yesterday, has no mark.
2. A refresh neither duplicates a line nor stacks a second mark on it.
3. A task ticked in today's note stays ticked and loses its mark.
4. A list rendered before the marks existed gains them on refresh, without a second copy of any
   task, and a task added to it by hand is kept.
5. The next day's rollover reads the tasks without their marks.

Ledger:
6. `state.py lint` prints "overdue:" for a current `next` whose "by <date>" has passed and for a
   `deadline` holding a past date, and its summary ends with the count; a `next` naming a future
   day, a past date in any other attribute, and a retracted `next` are not flagged.
7. The State Register lists it under "Overdue", and the opener flags it on a hot entity.
8. Appending what happened clears it.

With PALIMPSEST_TOOLS pointed at the tools from before this change, the marks in 1, check 4, the
flags in 6, and 7 and 8 fail. Checks 2, 3 and 5 pass there too, as the two controls do: they guard
what the marks could break (a doubled line, a lost tick, a mark rolled over as task text), which
cannot happen without marks.
"""
import json, os, re, subprocess, sys
from datetime import date, timedelta
from pathlib import Path
import _util
from _util import Checks, make_vault, run, write

CFG = json.dumps({"version": 1, "steps": {"pull": False}})
TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
ROLLED = "### ↩️ Rolled-over tasks"


def rolled(v: Path) -> list[str]:
    """The task lines of today's rolled-over list."""
    text = (v / "Daily" / f"{D(0)}.md").read_text(encoding="utf-8")
    a = text.index(ROLLED) + len(ROLLED)
    b = re.search(r"^(###|<!-- briefing:end -->)", text[a:], re.M)
    return [l for l in text[a:a + b.start() if b else None].splitlines() if l.startswith("- [")]


def line(lines: list[str], needle: str) -> str:
    return next((l for l in lines if needle in l), "")


def check_briefing(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    write(v, f"Daily/{D(-10)}.md", "# ten days ago\n\n- [ ] PARKED: raise the fleet throughput?\n")
    write(v, f"Daily/{D(-1)}.md", "# yesterday\n\n"
          "- [ ] PARKED: raise the fleet throughput?\n"
          f"- [ ] **By Fri {D(-3)} — email the recruiter** about the programme\n"
          f"- [ ] **{D(-3)}:** review the silence gate\n"
          f"- [ ] send the form by {D(5)}\n"
          "- [ ] a task first written yesterday\n")
    r = run(v, "briefing.py")
    ls = rolled(v) if r.returncode == 0 else []
    c.ok(line(ls, "email the recruiter").endswith(f"⏰ *due {D(-3)} passed*")
         and line(ls, "review the silence gate").endswith(f"⏰ *due {D(-3)} passed*")
         and line(ls, "PARKED").endswith(f"⏰ *open since {D(-10)}*"),
         "1. a rolled-over task whose day has passed, or that is a week old, ends in a mark saying so",
         f"rc={r.returncode} {ls} {r.stderr[-200:]}")
    c.ok(len(ls) == 5 and "⏰" not in line(ls, "send the form") and "⏰" not in line(ls, "first written yesterday"),
         "1. ...and a task naming a future day, or written yesterday, has none (held before)", str(ls))

    run(v, "briefing.py")
    ls2 = rolled(v)
    c.ok(ls2 == ls and not any(l.count("⏰") > 1 for l in ls2),
         "2. a refresh neither duplicates a line nor stacks a second mark", str(ls2))

    today = v / "Daily" / f"{D(0)}.md"
    today.write_text(today.read_text(encoding="utf-8").replace("- [ ] **By Fri", "- [x] **By Fri"), encoding="utf-8")
    run(v, "briefing.py")
    done = line(rolled(v), "email the recruiter")
    c.ok(done.startswith("- [x] ") and "⏰" not in done and len(rolled(v)) == 5,
         "3. a task ticked in today's note stays ticked and loses its mark", done)

    # A block as the previous briefing.py rendered it: no marks, and one task added by hand.
    text = today.read_text(encoding="utf-8")
    a = text.index(ROLLED) + len(ROLLED)
    b = a + re.search(r"^###", text[a:], re.M).start()
    plain = "\n" + "\n".join(re.sub(r"[ \t]*⏰ \*[^*]*\*$", "", l) for l in rolled(v)) + "\n- [ ] added by hand today\n\n"
    today.write_text(text[:a] + plain + text[b:], encoding="utf-8")
    run(v, "briefing.py")
    ls4 = rolled(v)
    c.ok(len(ls4) == 6 and line(ls4, "PARKED").endswith(f"⏰ *open since {D(-10)}*")
         and line(ls4, "added by hand today") == "- [ ] added by hand today"
         and line(ls4, "email the recruiter").startswith("- [x] "),
         "4. a list rendered before the marks existed gains them without a second copy of any task, "
         "and a task added by hand is kept", str(ls4))

    code = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport briefing\nfrom pathlib import Path\n"
            "print('\\n'.join(briefing.open_tasks(Path(sys.argv[2]))))\n")
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + code, str(v / "tools"), str(today)], cwd=v,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    tasks = r.stdout.splitlines()
    c.ok(len(tasks) == 5 and not any("⏰" in t for t in tasks) and "PARKED: raise the fleet throughput?" in tasks,
         "5. the next day's rollover reads the open tasks without their marks", f"{tasks} {r.stderr[-200:]}")


def st(v: Path, *args: str) -> subprocess.CompletedProcess:
    return run(v, "state.py", *args)


def check_ledger(c: Checks) -> None:
    v = make_vault()
    write(v, "palimpsest.json", CFG)
    for eid, hot in (("job-a", True), ("job-b", False), ("job-c", False), ("svc", False)):
        st(v, "register", eid, "--kind", "service", *(["--hot"] if hot else []))
    st(v, "add", "job-a", "next", f"email the recruiter by {D(-2)}: programme dates")
    st(v, "add", "job-b", "deadline", f"{D(-1)} (first role); {D(20)} (second role)")
    st(v, "add", "job-c", "next", f"send the form by {D(5)}")                      # a future day
    st(v, "add", "svc", "status", f"degraded; revisit by {D(-9)}")                 # not an intention
    st(v, "add", "svc", "next", f"rotate the key by {D(-4)}")
    st(v, "retract", "svc", "next")
    r = st(v, "lint")
    out = r.stdout
    c.ok(f"overdue: job-a.next names {D(-2)}" in out and f"overdue: job-b.deadline names {D(-1)}" in out
         and re.search(r"^state: .* overdue=2$", out, re.M) is not None,
         "6. lint flags a `next` whose 'by <date>' has passed and a `deadline` holding a past date, and counts them",
         out[-600:] + r.stderr[-200:])
    c.ok("job-c" not in out and "svc" not in out,
         "6. ...and not a `next` naming a future day, a past date in another attribute, or a retracted `next`",
         out[-600:])

    reg = next(iter(v.rglob("State Register.md")), None) or next(iter((v / "State").glob("Register.md")), None)
    text = reg.read_text(encoding="utf-8") if reg else ""
    block = text.split("## ⏰ Overdue", 1)[1].split("\n## ", 1)[0] if "## ⏰ Overdue" in text else ""
    op = st(v, "show", "--hot", "--opener").stdout
    c.ok("**job-a.next**" in block and "**job-b.deadline**" in block and "job-c" not in block
         and "⏰ overdue" in op and "job-a" in op,
         "7. the State Register lists it under 'Overdue', and the opener flags it on a hot entity",
         f"register={block[:300]!r} opener={op[-300:]!r}")

    st(v, "add", "job-a", "next", f"email sent {D(0)}; awaiting a reply")
    st(v, "add", "job-b", "deadline", f"{D(20)} (second role); the first was missed")
    r = st(v, "lint")
    c.ok(re.search(r"^state: .* overdue=0$", r.stdout, re.M) is not None and "overdue:" not in r.stdout,
         "8. appending what happened clears it", r.stdout[-400:])


def main() -> int:
    c = Checks("stale tasks say so (2026-10-04)")
    check_briefing(c)
    check_ledger(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
