"""Four fixes the vault this harness came from already had, brought over on 2026-10-06.

Each was made in that vault after this repository was split from it, and never carried across.

1. The extractors put their instructions ahead of the transcript and nothing after it. A long
   transcript that ended on a question or a goodnight was answered ("Sleep well.") instead of
   mined. Both now restate the task after the transcript, where the model reads it last.
2. A failed `claude` call was reported by its stderr when it had any. The CLI puts the reason
   (an API, model or usage error) on stdout, and a warning on stderr stood in for it. The reason
   is reported first, then stderr. And the two tool names only an older CLI has (LS, MultiEdit),
   each of which makes a current CLI print such a warning, are named in the legacy fallback only.
3. A deny-listed phone number was matched only between digit boundaries. Listed as its national
   digits and written with a trunk or country code touching it ("85550100123", "+75550100123"),
   it was neither redacted from a recorded note nor blocked at commit. Redaction now masks the
   digits wherever they stand; a commit is blocked on the whole number, with "+" and one to three
   digits or one bare digit allowed in front, and warned about when the digits sit inside a longer
   number (a chat id, a timestamp), which is not blocked.
4. A deny list whose last line had no newline, with a Cyrillic term then appended as UTF-16
   (PowerShell 5.1's `>>`), was read as one line of mojibake: both terms were lost, and the
   guards refused to run until the file was repaired. The UTF-16 part is now split off where it
   starts for a first letter up to U+05FF, not only an ASCII one, and both terms are kept.

With PALIMPSEST_TOOLS pointed at the tools from before this change, every check fails except those
marked "(held before)". Every model call goes to a fake `claude` first on PATH. The phone number
is from the range reserved for fiction.
"""
import json, os, subprocess, sys
from pathlib import Path
from _util import Checks, git, make_vault, run, stub, stub_path, write

CONV = "40 Resources/Claude Conversations/Claude Code/demo/"
TALK = "---\ntype: x\n---\n\n# herons\n\na talk about heron ledgers\n\nUser: so, sleep well?\n"
KINDS = (("extract_notes", "10 Notes"), ("extract_skills", "Skills/_proposed"))

# Logs each prompt and how it was started. STUB_MODE: "empty" answers [], "fail" is an API error
# as the CLI gives one (reason on stdout, a warning on stderr, exit 1), "old" is a CLI from
# before --tools (that option is an argv error; without it, it answers []).
STUB = ("import json, os, sys\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8', 'replace')\n"
        "with open(os.environ['STUB_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}) + '\\n')\n"
        "mode = os.environ['STUB_MODE']\n"
        "if mode == 'old' and '--tools' in sys.argv:\n"
        "    sys.stderr.write(\"error: unknown option '--tools'\\n\")\n"
        "    sys.exit(1)\n"
        "if mode == 'fail':\n"
        "    sys.stderr.write('Warning: a rule in --disallowedTools matches no known tool\\n')\n"
        "    print('API Error: the usage limit was reached')\n"
        "    sys.exit(1)\n"
        "print('[]')\n")


def extract(mod: str, mode: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
    v = make_vault()
    stub(v / "fakebin", "claude", STUB)
    write(v, CONV + "2026-09-01 herons (aaaaaaaa).md", TALK)
    r = run(v, f"{mod}.py", env={"PATH": stub_path(v / "fakebin"), "STUB_LOG": str(v / "calls.log"), "STUB_MODE": mode})
    log = v / "calls.log"
    return r, [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def denied(argv: list[str]) -> list[str]:
    return argv[argv.index("--disallowedTools") + 1].split(",") if "--disallowedTools" in argv[:-1] else []


def check_extraction(c: Checks) -> None:
    for mod, _ in KINDS:
        r, calls = extract(mod, "empty")
        prompt = calls[0]["prompt"] if calls else ""
        after = prompt.split("so, sleep well?", 1)[1] if "so, sleep well?" in prompt else ""
        c.ok(r.returncode == 0 and len(calls) == 1 and "===END CONVERSATION===" in after
             and "data to mine" in after and prompt.rstrip().endswith("([] if there is nothing to keep)."),
             f"1. {mod}: the task is restated after the transcript, and is the last thing in the prompt",
             f"rc={r.returncode} tail={prompt[-200:]!r}")

        r, calls = extract(mod, "fail")
        c.ok(r.returncode != 0 and "the usage limit was reached" in r.stdout and "matches no known tool" in r.stdout,
             f"2. {mod}: a failed call is reported by the reason on stdout, with the stderr warning after it",
             f"rc={r.returncode} {r.stdout[-400:]}")

        r, calls = extract(mod, "empty")
        names = denied(calls[0]["argv"]) if calls else []
        c.ok("Bash" in names and "LS" not in names and "MultiEdit" not in names,
             f"2. {mod}: a current CLI is not handed tool names it does not know", str(names))
        r, calls = extract(mod, "old")
        legacy = [denied(k["argv"]) for k in calls if "--tools" not in k["argv"]]
        c.ok(r.returncode == 0 and bool(legacy) and all({"Bash", "LS", "MultiEdit"} <= set(n) for n in legacy),
             f"2. {mod}: the fallback for an older CLI still names LS and MultiEdit (held before)",
             f"rc={r.returncode} {legacy} {r.stdout[-200:]}")


NUMBER = "5550100123"


def pii_vault(terms: bytes) -> Path:
    v = make_vault()
    write(v, ".gitignore", "tools/.redact_terms.txt\n")
    (v / "tools" / ".redact_terms.txt").write_bytes(terms)
    return v


def redact(v: Path, text: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(v / "tools" / "redact.py")], cwd=v, input=text.encode("utf-8"),
                       capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def scan(v: Path, text: str) -> subprocess.CompletedProcess:
    write(v, "10 Notes/n.md", text)
    git(v, "add", "-A")
    return run(v, "scan_pii.py")


def check_phone(c: Checks) -> None:
    v = pii_vault(NUMBER.encode() + b"\n")
    rc, out = redact(v, "ring 85550100123, +75550100123 or +7 (555) 010-01-23; chat id 9955501001234 today\n")
    c.ok(rc == 0 and "0100123" not in out and "010-01-23" not in out and "today" in out,
         "3. a listed number is redacted with a trunk or country code touching it, and inside a longer number",
         f"rc={rc} {out}")
    for text, how in (("ring 85550100123\n", "a trunk code"), ("ring +75550100123\n", "a country code")):
        r = scan(v, text)
        c.ok(r.returncode == 1 and NUMBER not in r.stdout, f"3. a commit is blocked on the number with {how} touching it",
             f"rc={r.returncode} {r.stdout[-300:]}")
    r = scan(v, "chat id 9955501001234 and timestamp 15550100123999\n")
    c.ok(r.returncode == 0 and "inside a longer number" in r.stdout and NUMBER not in r.stdout,
         "3. the digits inside a longer number do not block the commit, and are warned about",
         f"rc={r.returncode} {r.stdout[-300:]}")
    r = scan(v, "ring 555 010-01-23\n")
    c.ok(r.returncode == 1, "3. the number on its own, in another spelling, still blocks (held before)",
         f"rc={r.returncode} {r.stdout[-300:]}")


def check_deny_list(c: Checks) -> None:
    v = pii_vault(b"Smithson" + "Иванов\r\n".encode("utf-16"))        # no newline, then PowerShell's >>
    rc, out = redact(v, "written by Smithson and by Иванов today\n")
    c.ok(rc == 0 and "Smithson" not in out and "Иванов" not in out and "today" in out,
         "4. a Cyrillic term appended to the deny list as UTF-16 is kept, with the term before it",
         f"rc={rc} {out}")
    r = scan(v, "a note that names Иванов\n")
    c.ok(r.returncode == 1 and "not clean UTF-8" not in r.stdout + r.stderr and "Иванов" not in r.stdout,
         "4. ...and the commit guard blocks on that term, not on the state of the list",
         f"rc={r.returncode} {(r.stdout + r.stderr)[-300:]}")


def main() -> int:
    c = Checks("from the vault, 2026-10-06: prompt tail, call errors, phone numbers, appended deny terms")
    check_extraction(c)
    check_phone(c)
    check_deny_list(c)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
