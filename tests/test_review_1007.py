"""Regressions for the review of 2026-10-07: two of its three findings.

1. extract_notes.py and extract_skills.py listed the conversation notes with rglob and read each
   one, link or not. A `.md` symlink in the conversations folder that points outside the vault
   had its target's contents sent to the model, and distilled into notes the vault then syncs.
   bench_retrieval.py --gen did the same with a linked note in "10 Notes". A file whose real
   path is not inside the vault is now skipped and named, as ask.py already did, and an
   extraction run that skipped one does not report itself clean.
2. state.py's fold counted a contradiction as resolved only by a fact with a strictly later
   timestamp, and timestamps have one-second precision. A fact appended in the same second as
   the contradiction it resolves (a script, an agent's two commands) updated the value and left
   the conflict open until some later fact happened to land. A contradiction now carries the
   key's sequence number, as facts do, and a same-second fact with that number or a higher one
   resolves it; one recorded before the contradiction in that second does not. And confirming
   the value that stands, with the date it already has, was a no-op that wrote nothing, so the
   contradiction it answered stayed open; with a conflict open it is now recorded.

The third finding (a timed-out sync step leaves the model call it started running) is not fixed
here: see "Limitations, honestly" in the README for why killing the step's whole process tree
was tried and withdrawn.

With PALIMPSEST_TOOLS pointed at the tools from before this change, every check fails except those
marked "(held before)". Every model call goes to a fake `claude` first on PATH.
"""
import json, os, subprocess, sys
from pathlib import Path
from _util import UTF8_STDIO, Checks, can_symlink, make_vault, run, stub, stub_path, tempdir, write

CONV = "40 Resources/Claude Conversations/Claude Code/demo/"
TALK = "---\ntype: x\n---\n\n# herons\n\na talk about heron ledgers and walrus exports\n"
MARKER = "OUTSIDE-MARKER-7731"

# Logs each prompt; answers bench_retrieval's GEN_PROMPT with one row, anything else with [].
LOG_STUB = ("import json, os, sys\n"
            "prompt = sys.stdin.buffer.read().decode('utf-8', 'replace')\n"
            "with open(os.environ['STUB_LOG'], 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(prompt) + '\\n')\n"
            "if 'retrieval benchmark' in prompt:\n"
            "    print(json.dumps([{'id': 0, 'en': 'how do striped animals hide', 'ru': 'kak polosatye pryachutsya'}]))\n"
            "else:\n"
            "    print('[]')\n")


def prompts(v: Path) -> list[str]:
    log = v / "calls.log"
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def check_links(c: Checks) -> None:
    if not can_symlink():
        for what in ("1. a conversation note that links outside the vault is not sent to the model (both extractors)",
                     "1. ...nor is a note under a linked folder, and the run names what it skipped",
                     "1. bench_retrieval.py --gen does not send a linked note either"):
            c.skip(what, "this process may not create symlinks")
        return
    outside = tempdir("palimpsest-outside-")
    private = outside / "2026-09-05 private (cccccccc).md"
    private.write_text(f"# private\n\n{MARKER} " + "a file that is not in the vault. " * 20, encoding="utf-8")
    for mod, outdir in (("extract_notes", "10 Notes"), ("extract_skills", "Skills/_proposed")):
        v = make_vault()
        stub(v / "fakebin", "claude", LOG_STUB)
        env = {"PATH": stub_path(v / "fakebin"), "STUB_LOG": str(v / "calls.log")}
        (v / CONV).mkdir(parents=True)
        os.symlink(private, v / CONV / "2026-09-01 linked (aaaaaaaa).md")
        os.symlink(outside, v / CONV / "linked-folder", target_is_directory=True)
        write(v, CONV + "2026-09-02 herons (bbbbbbbb).md", TALK)
        r = run(v, f"{mod}.py", env=env)
        sent, out = prompts(v), r.stdout + r.stderr
        c.ok(not any(MARKER in p for p in sent) and sum("heron ledgers" in p for p in sent) == 1,
             f"1. {mod}: a conversation note that links outside the vault is not sent to the model; the one in the vault is",
             f"rc={r.returncode} sent={len(sent)} leaked={sum(MARKER in p for p in sent)} {out[-300:]}")
        c.ok(r.returncode != 0 and "linked (aaaaaaaa).md" in out and "outside the vault" in out and "Traceback" not in out,
             f"1. {mod}: ...and the run names the link it skipped and does not report itself clean",
             f"rc={r.returncode} {out[-400:]}")

    v = make_vault()
    stub(v / "fakebin", "claude", LOG_STUB)
    write(v, "10 Notes/Zebra.md", "# Zebra\n\n" + "Zebras have stripes that break up their outline. " * 12)
    os.symlink(private, v / "10 Notes" / "Linked.md")
    r = run(v, "bench_retrieval.py", "--gen", "5", env={"PATH": stub_path(v / "fakebin"), "STUB_LOG": str(v / "calls.log")})
    sent = prompts(v)
    c.ok(r.returncode == 0 and len(sent) == 1 and "Zebras have stripes" in sent[0] and MARKER not in sent[0],
         "1. bench_retrieval.py --gen does not send a linked note either",
         f"rc={r.returncode} sent={len(sent)} leaked={sum(MARKER in p for p in sent)} {(r.stdout + r.stderr)[-300:]}")


# state.py's own commands, each run with the clock held at a given second of one minute.
CLOCKED = UTF8_STDIO + ("import json, sys\n"
                        "from datetime import datetime, timezone\n"
                        "sys.path.insert(0, 'tools')\n"
                        "import state as m\n"
                        "ids = {}\n"
                        "for second, args in json.loads(sys.argv[1]):\n"
                        "    m.utcnow = lambda s=second: datetime(2026, 10, 7, 3, 0, s, tzinfo=timezone.utc)\n"
                        "    args = [ids.get(a, a) for a in args]\n"
                        "    sys.argv = ['state.py', *args]\n"
                        "    try:\n"
                        "        m.main()\n"
                        "    except SystemExit as e:\n"
                        "        if e.code:\n"
                        "            print('EXIT', e.code, args)\n"
                        "    facts = [f for f in m.load_facts()[0] if f.get('kind') != 'contradicts']\n"
                        "    ids = {f'@{i}': f['id'] for i, f in enumerate(facts)}\n"
                        "rec = m.fold(m.load_facts()[0])['svc']['host']\n"
                        "print('RESULT ' + json.dumps({'value': rec.get('value'), 'conflicts': len(rec.get('conflicts') or [])}))\n")


def folded(steps: list) -> dict:
    v = make_vault()
    write(v, "palimpsest.json", json.dumps({"version": 1, "steps": {"pull": False}}))
    setup = [[0, ["register", "svc", "--kind", "service"]], [1, ["add", "svc", "host", "box"]],
             [2, ["add", "svc", "host", "vps"]]]
    r = subprocess.run([sys.executable, "-c", CLOCKED, json.dumps(setup + steps)], cwd=v, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), "")
    return json.loads(line[7:]) if line else {"error": (r.stdout + r.stderr)[-400:]}


def check_same_second(c: Checks) -> None:
    contradict = ["contradict", "@0", "@1", "--reason", "two hosts cannot both be right"]
    got = folded([[5, contradict]])
    c.ok(got == {"value": "vps", "conflicts": 1}, "2. a contradiction nobody has answered stays open (held before)", str(got))
    got = folded([[5, contradict], [6, ["add", "svc", "host", "cloud"]]])
    c.ok(got == {"value": "cloud", "conflicts": 0}, "2. a fact added a second later resolves it (held before)", str(got))
    got = folded([[5, contradict], [5, ["add", "svc", "host", "cloud"]]])
    c.ok(got == {"value": "cloud", "conflicts": 0}, "2. a fact added in the same second resolves it too", str(got))
    got = folded([[5, ["add", "svc", "host", "edge"]], [5, contradict]])
    c.ok(got == {"value": "edge", "conflicts": 1},
         "2. a fact recorded in that second BEFORE the contradiction does not resolve it (held before)", str(got))
    got = folded([[5, contradict], [6, ["add", "svc", "host", "vps", "--since", "2026-10-07T03:00:02Z"]]])
    c.ok(got == {"value": "vps", "conflicts": 0},
         "2. confirming the value that stands, with the date it already has, resolves the contradiction", str(got))
    got = folded([[5, ["add", "svc", "host", "edge"]], [5, contradict], [5, ["add", "svc", "host", "edge"]]])
    c.ok(got == {"value": "edge", "conflicts": 0}, "2. ...and so does confirming it within the same second", str(got))


def main() -> int:
    c = Checks("review 2026-10-07: links out of the vault, same-second resolutions")
    check_links(c)
    check_same_second(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
