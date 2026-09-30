"""Regressions for the sixth review (2026-09-30): extraction and import.

One check per finding; each failed on ba54bc9. No model is called: a fake `claude` first on
PATH logs every call (argv, cwd, whether the prompt carried an ALREADY CAPTURED list) and
answers from FAKE_REPLY, or in "grow" mode with a new title per call unless told what is
already captured — which is what a real model rewording its titles looks like.

 1. A failed conversation-note write no longer truncates the existing note (lone surrogate).
 2. claude.ai conversations without a uuid get one note each instead of overwriting one.
 3. The checkpoint survives a lost state + new mtime (another machine, a rebase): no re-extraction.
 4. A conversation that grew is re-extracted with its earlier notes listed as already captured.
 5. A non-string optional field (list trigger, nested-list tag) does not crash the run.
 6. A corrupt checkpoint stops the run loudly instead of re-extracting everything.
 7. A single turn longer than the chunk limit is split.
 8. The skill trigger and tags are valid YAML whatever the model wrote.
 9. A second extraction run while one holds the lock makes no model calls.
10. The skill near-duplicate gate sees Cyrillic words.
11. The model runs outside the vault with its tools denied.
12. A title starting with "_" or "." does not produce a hidden note.
13. A long CJK title fits NAME_MAX in bytes.
14. Steps are not split inside a `code span` or on a bare ';'.
15. The claude executable is resolved through shutil.which (PATHEXT on Windows).
16. A note holding a carriage return is not rewritten when unchanged.
17. Conversation dates are local, not UTC.
18. A CRLF note (the box, before this change) is left alone, and an old checkpoint still matches
    it after its line endings change: no re-extraction on upgrade.
19. A grown conversation that yields nothing new on one machine is not re-sent on the other.
20. A lone surrogate in a model's title or body is written, not a failure retried forever.

Checks 1, 8 and 11 also guard what the first pass of these fixes broke (the file mode, legacy
triggers that happen to parse as JSON, the per-call temp cwd), and still fail on ba54bc9.
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile
from pathlib import Path
import _util
from _util import Checks, run, write

VAULTS: list[Path] = []


def make_vault() -> Path:
    VAULTS.append(_util.make_vault())
    return VAULTS[-1]

FAKE = r'''#!{py}
import json, os, sys
prompt = sys.stdin.read()
log = os.environ["FAKE_LOG"]
n = sum(1 for _ in open(log)) + 1 if os.path.exists(log) else 1
with open(log, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                         "mcp": os.environ.get("ENABLE_CLAUDEAI_MCP_SERVERS"),
                         "captured": "ALREADY CAPTURED" in prompt, "prompt": prompt[-2000:]}}) + "\n")
if os.environ.get("FAKE_OLD_CLI") and "--tools" in sys.argv:
    sys.stderr.write("error: unknown option '--tools'\n")
    sys.exit(1)
if os.environ.get("FAKE_MODE") == "grow":
    if "ALREADY CAPTURED" in prompt:
        print("[]")
    elif os.environ.get("FAKE_KIND") == "skills":
        print(json.dumps([{{"name": f"Skill number {{n}}", "when_to_use": "when x", "steps": "do y",
                           "why": "because z", "tags": ["t"]}}]))
    else:
        print(json.dumps([{{"title": f"Insight number {{n}}", "body": "A body.", "tags": ["t"]}}]))
else:
    print(os.environ.get("FAKE_REPLY", "[]"))
'''

CONV = "40 Resources/Claude Conversations/Claude Code/demo"


def load(v, name):
    sys.path.insert(0, str(v / "tools"))
    for m in ("extract_notes", "extract_skills", "import_claude", "triage_skills", name):
        sys.modules.pop(m, None)
    return __import__(name)


def fake_env(v: Path, **extra) -> dict:
    fb = v / "fakebin"
    fc = write(v, "fakebin/claude", FAKE.format(py=sys.executable))
    fc.chmod(fc.stat().st_mode | stat.S_IEXEC)
    log = v / "fake.log"
    # A temp dir of its own, outside the vault: the extractors keep a fixed cwd under it.
    VAULTS.append(Path(tempfile.mkdtemp(prefix="palimpsest-tmp-")))
    return {"PATH": f"{fb}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log),
            "TMPDIR": str(VAULTS[-1]), "TEMP": str(VAULTS[-1]), "TMP": str(VAULTS[-1]), **extra}


def calls(v: Path) -> list[dict]:
    log = v / "fake.log"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def conv(v: Path, text: str = "hello there\n", name: str = "2026-09-01 talk (aaaaaaaa).md") -> Path:
    return write(v, f"{CONV}/{name}", "---\ntype: claude-conversation\n---\n\n# talk\n\n" + text)


def notes(v: Path, d: str = "10 Notes") -> list[Path]:
    return sorted((v / d).glob("*.md")) if (v / d).exists() else []


def guard(c: Checks, what: str, fn):
    try:
        fn()
    except Exception as e:
        c.ok(False, what, f"{type(e).__name__}: {e}")


def main() -> int:
    c = Checks("review 2026-09-30: extraction and import")

    # 1. a failed write must not truncate the existing note
    def t1():
        v = make_vault()
        ic = load(v, "import_claude")
        folder = v / CONV
        write(v, f"{CONV}/n (s1).md", "earlier turns, keep me\n")
        err = ""
        try:
            ic.write_note(folder, "n (s1).md", {"type": "x"}, "cut emoji \ud83d here")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        after = (folder / "n (s1).md").read_text(encoding="utf-8", errors="replace")
        # ... and an atomic write keeps the modes a plain write gave: the note's own when it is
        # rewritten, 0666 less the umask for a new one (mkstemp's 0600 was carried over).
        mask = os.umask(0)
        os.umask(mask)
        kept = folder / "kept (s3).md"
        write(v, f"{CONV}/kept (s3).md", "old text\n").chmod(0o640)
        modes = []
        for f in ("kept (s3).md", "new (s4).md"):
            try:
                ic.write_note(folder, f, {"type": "x"}, "fresh text")
                modes.append(stat.S_IMODE((folder / f).stat().st_mode))
            except Exception as e:
                modes.append(f"{type(e).__name__}: {e}")
        c.ok(not err and "cut emoji" in after and after.strip() != "" and "fresh" in kept.read_text()
             and modes == [0o640, 0o666 & ~mask],
             "1. a note with a lone surrogate is written, never left truncated, and modes are kept",
             (err or repr(after[:80])) + f" modes={[oct(m) if isinstance(m, int) else m for m in modes]}")
    guard(c, "1. a note with a lone surrogate is written, never left truncated, and modes are kept", t1)

    # 2. uuid-less web conversations
    def t2():
        v = make_vault()
        ex = write(v, "export.json", json.dumps([
            {"name": "First chat", "created_at": "2026-09-01T10:00:00Z",
             "messages": [{"role": "user", "text": "alpha question"}]},
            {"name": "Second chat", "created_at": "2026-09-02T10:00:00Z",
             "messages": [{"role": "user", "text": "beta question"}]}]))
        run(v, "import_claude.py", "web", str(ex))
        got = notes(v, "40 Resources/Claude Conversations/claude.ai")
        text = " ".join(p.read_text() for p in got)
        c.ok(len(got) == 2 and "alpha" in text and "beta" in text,
             "2. two uuid-less web conversations make two notes", str([p.name for p in got]))
    guard(c, "2. two uuid-less web conversations make two notes", t2)

    # 3. another machine (no local state) or a rebase (new mtime) does not re-extract
    def t3():
        ok, detail = True, []
        for script, state, kind, d in (("extract_notes.py", ".extract_state.json", "notes", "10 Notes"),
                                       ("extract_skills.py", ".extract_skills_state.json", "skills",
                                        "Skills/_proposed")):
            v = make_vault()
            env = fake_env(v, FAKE_MODE="grow", FAKE_KIND=kind)
            src = conv(v)
            run(v, script, env=env)
            (v / "tools" / state).unlink()                       # machine B: no checkpoint of its own
            os.utime(src, (1_000_000_000, 1_000_000_000))        # git gives it a fresh mtime
            r = run(v, script, env=env)
            n = len(calls(v))
            ok &= n == 1 and len(notes(v, d)) == 1
            detail.append(f"{script}: {n} call(s), {len(notes(v, d))} file(s) {r.stdout[-200:]}")
        c.ok(ok, "3. an extraction done elsewhere is not repeated (no local state, new mtime)", " | ".join(detail))
    guard(c, "3. an extraction done elsewhere is not repeated (no local state, new mtime)", t3)

    # 4. a conversation that grew is re-extracted knowing what it already yielded
    def t4():
        v = make_vault()
        env = fake_env(v, FAKE_MODE="grow")
        src = conv(v)
        run(v, "extract_notes.py", env=env)
        with src.open("a") as fh:
            fh.write("\n---\n\nand a later turn\n")
        run(v, "extract_notes.py", env=env)
        cs = calls(v)
        c.ok(len(cs) == 2 and cs[1]["captured"] and "Insight number 1" in cs[1]["prompt"]
             and len(notes(v)) == 1,
             "4. a grown conversation's earlier notes are passed as already captured",
             f"{len(cs)} calls, {[p.name for p in notes(v)]}")
    guard(c, "4. a grown conversation's earlier notes are passed as already captured", t4)

    # 5. non-string optional fields
    def t5():
        v = make_vault()
        conv(v)
        rs = []
        for script, reply in (
                ("extract_skills.py", [{"name": "Odd fields skill", "steps": "a; b", "when_to_use": ["when", "x"],
                                         "why": 5, "tags": [["nested"], "ok"]}]),
                ("extract_notes.py", [{"title": "Odd fields note", "body": "b", "tags": [["nested"], "ok"],
                                        "volatility": ["dated"]}])):
            r = run(v, script, env=fake_env(v, FAKE_REPLY=json.dumps(reply)))
            rs.append((script, r.returncode, "Traceback" in r.stderr, r.stderr[-200:]))
        wrote = (v / "Skills/_proposed/Odd fields skill.md").exists() and (v / "10 Notes/Odd fields note.md").exists()
        c.ok(wrote and all(rc == 0 and not tb for _, rc, tb, _ in rs),
             "5. list/number/nested fields are coerced, not a crash", str(rs))
    guard(c, "5. list/number/nested fields are coerced, not a crash", t5)

    # 6. corrupt checkpoint
    def t6():
        v = make_vault()
        conv(v)
        bad = '{"40 Resources/x.md": {"sig": "1:2"'
        write(v, "tools/.extract_state.json", bad)
        r = run(v, "extract_notes.py", env=fake_env(v))
        c.ok(r.returncode != 0 and not calls(v) and (v / "tools/.extract_state.json").read_text() == bad,
             "6. a corrupt checkpoint stops the run, keeps the file, calls no model",
             f"rc={r.returncode} calls={len(calls(v))} {r.stdout[-200:]}")
    guard(c, "6. a corrupt checkpoint stops the run, keeps the file, calls no model", t6)

    # 7. one oversized turn is split
    def t7():
        v = make_vault()
        en, es = load(v, "extract_notes"), load(v, "extract_skills")
        text = "small turn\n---\n" + "\n".join("line %05d " % i + "x" * 40 for i in range(200)) + "\n---\nend"
        res = []
        for mod in (en, es):
            chunks = mod.chunk_transcript(text, 1000)
            res.append((max(map(len, chunks)), "line 00199" in "".join(chunks)))
        c.ok(all(m <= 1000 and kept for m, kept in res), "7. a turn over the limit is split, nothing lost", str(res))
    guard(c, "7. a turn over the limit is split, nothing lost", t7)

    # 8. YAML-safe trigger and tags, and triage reads the trigger back
    def t8():
        v = make_vault()
        es = load(v, "extract_skills")
        es.PROPOSED_DIR = v / "Skills" / "_proposed"
        when = 'When pm2 logs show "EADDRINUSE" for C:\\Users\\svc\ntrigger: injected'
        es.write_proposed_skill({"name": "Free the port", "steps": "find the pid\nkill it", "when_to_use": when,
                                 "why": "the port is held", "tags": ["*nix", "@types"]},
                                v / "src.md", "2026-09-30", False)
        txt = (es.PROPOSED_DIR / "Free the port.md").read_text()
        front = txt.split("---\n")[1]
        try:
            import yaml
            fm = yaml.safe_load(front)
        except ImportError:
            fm = {"trigger": json.loads(re.search(r"^trigger: (.*)$", front, re.M).group(1)),
                  "tags": re.findall(r"^  - (.*)$", front, re.M)}
        ts = load(v, "triage_skills")
        # An older proposal holds the trigger raw; one that happens to be valid JSON must not be
        # decoded ("\\n" and "\\t" in a Windows path became a newline and a tab).
        legacy = {}
        for raw in ('When C:\\new\\tools breaks', 'When "quoted" and C:\\Users\\x fails'):
            lp = write(v, "Skills/_proposed/Legacy.md", f'---\ntype: skill\ntrigger: "{raw}"\n---\n\n# Legacy\n')
            legacy[raw] = ts.parse(lp)["trigger"]
        c.ok(isinstance(fm, dict) and "EADDRINUSE" in fm.get("trigger", "") and "\\Users" in fm["trigger"]
             and "injected" not in fm and len(fm.get("tags", [])) == 3
             and ts.parse(es.PROPOSED_DIR / "Free the port.md")["trigger"] == fm["trigger"]
             and all(k == got for k, got in legacy.items()),
             "8. the trigger and tags are valid YAML; triage reads it and legacy raw triggers back",
             front + " | " + repr(legacy))
    guard(c, "8. the trigger and tags are valid YAML; triage reads it and legacy raw triggers back", t8)

    # 9. the extraction lock
    def t9():
        v = make_vault()
        conv(v)
        en = load(v, "extract_notes")
        held = getattr(en, "hold_lock", lambda n: False)("extract_notes.lock")
        r = run(v, "extract_notes.py", env=fake_env(v))
        c.ok(held and not calls(v), "9. a run overlapping a locked one makes no model calls",
             f"held={held} calls={len(calls(v))} {r.stdout[-200:]}")
        for fh in getattr(en, "_LOCKS", []):
            fh.close()
    guard(c, "9. a run overlapping a locked one makes no model calls", t9)

    # 10. Cyrillic words count for the skill near-duplicate gate
    def t10():
        v = make_vault()
        es = load(v, "extract_skills")
        es.PROPOSED_DIR = v / "Skills" / "_proposed"
        es._INDEX = None
        a = {"name": "Перезапускай бота через ecosystem.config.js",
             "steps": "Выполни pm2 start ecosystem.config.js; не запускай скрипт напрямую",
             "why": "pm2 save сохраняет только то, что запущено", "when_to_use": "бот упал"}
        b = {"name": "Не делай pm2 save раньше времени",
             "steps": "Сначала подними всю топологию из ecosystem.config.js; только потом pm2 save",
             "why": "иначе дамп запомнит неполный набор процессов", "when_to_use": "меняешь топологию"}
        es.write_proposed_skill(a, v / "src.md", "2026-09-30", False)
        dup = es.near_duplicate_of(b, 0.45)
        c.ok(dup is None, "10. two different Russian skills sharing tool names are not called duplicates", str(dup))
    guard(c, "10. two different Russian skills sharing tool names are not called duplicates", t10)

    # 11. the model call runs outside the vault, in one fixed dir, with no tools and no MCP
    def t11():
        v = make_vault()
        conv(v)
        env = fake_env(v)
        for script in ("extract_notes.py", "extract_skills.py"):
            run(v, script, env=env)
        cs, res = calls(v), []
        for cl in cs:
            cwd = Path(cl["cwd"]).resolve()
            argv = cl["argv"]
            denied = argv[argv.index("--disallowedTools") + 1] if "--disallowedTools" in argv else ""
            res.append(v.resolve() not in (cwd, *cwd.parents)
                       and argv[-2:] == ["--tools", ""] and "--strict-mcp-config" in argv
                       and "--no-session-persistence" in argv and cl["mcp"] == "false"
                       and all(t in denied.split(",") for t in ("Bash", "Read", "Grep", "Write")))
        # One stable directory, not a fresh temp dir per call that a kill mid-call leaks.
        stable = len({cl["cwd"] for cl in cs}) == 1 and all(Path(cl["cwd"]).is_dir() for cl in cs)
        # An older CLI that rejects --tools falls back to the denylist instead of failing everything.
        v2 = make_vault()
        conv(v2)
        r2 = run(v2, "extract_notes.py", env=fake_env(v2, FAKE_OLD_CLI="1"))
        c2 = calls(v2)
        fell_back = (r2.returncode == 0 and len(c2) == 2 and "--tools" not in c2[1]["argv"]
                     and "--disallowedTools" in c2[1]["argv"])
        # A dir someone else could write to (shared /tmp) is refused rather than used.
        refused = True
        if hasattr(os, "getuid") and cs:
            Path(cs[0]["cwd"]).chmod(0o777)
            before = len(calls(v))
            r3 = run(v, "extract_skills.py", "--force", env=env)
            refused = r3.returncode != 0 and len(calls(v)) == before and "private" in r3.stdout
            Path(cs[0]["cwd"]).chmod(0o700)
        c.ok(len(res) == 2 and all(res) and stable and fell_back and refused,
             "11. claude -p runs outside the vault in one fixed dir, with no tools, MCP or transcript",
             f"res={res} stable={stable} fell_back={fell_back} refused={refused} "
             + str([(x["cwd"], x["argv"]) for x in cs]) + r2.stdout[-200:])
    guard(c, "11. claude -p runs outside the vault in one fixed dir, with no tools, MCP or transcript", t11)

    # 12. / 13. file names
    def t12():
        v = make_vault()
        en, es = load(v, "extract_notes"), load(v, "extract_skills")
        names = [m.sanitize(t) for m in (en, es) for t in ("__slots__ cut per-instance memory",
                                                            ".gitignore does not untrack files")]
        c.ok(not any(n.startswith(("_", ".")) for n in names), "12. no hidden file names from '_' or '.' titles",
             str(names))
    guard(c, "12. no hidden file names from '_' or '.' titles", t12)

    def t13():
        v = make_vault()
        en, es = load(v, "extract_notes"), load(v, "extract_skills")
        sizes = [len((m.sanitize(t) + ".md").encode()) for m in (en, es) for t in ("漢字" * 45, "🙂" * 80)]
        c.ok(all(s <= 255 for s in sizes), "13. long CJK / emoji titles fit NAME_MAX in bytes", str(sizes))
    guard(c, "13. long CJK / emoji titles fit NAME_MAX in bytes", t13)

    # 14. steps split
    def t14():
        v = make_vault()
        es = load(v, "extract_skills")
        es.PROPOSED_DIR = v / "Skills" / "_proposed"
        steps = 'Run `for f in *.log; do gzip "$f"; done` in the log dir\nOn Windows set PATH=C:\\bin;%PATH% first'
        es.write_proposed_skill({"name": "Compress logs", "steps": steps, "when_to_use": "logs pile up",
                                 "why": "disk"}, v / "src.md", "2026-09-30", False)
        body = (es.PROPOSED_DIR / "Compress logs.md").read_text()
        bullets = re.search(r"## Steps\n(.*?)\n\n", body, re.S).group(1).splitlines()
        c.ok(len(bullets) == 2 and "`for f in *.log; do gzip \"$f\"; done`" in bullets[0]
             and "PATH=C:\\bin;%PATH%" in bullets[1], "14. steps keep code spans and bare ';' intact", str(bullets))
    guard(c, "14. steps keep code spans and bare ';' intact", t14)

    # 15. the executable is resolved with shutil.which
    def t15():
        v = make_vault()
        env = fake_env(v)
        os.environ["FAKE_LOG"] = env["FAKE_LOG"]
        en = load(v, "extract_notes")
        empty = tempfile.mkdtemp()
        old_path, old_which = os.environ["PATH"], shutil.which
        # a PATH on which a bare "claude" does not resolve, as with npm's claude.cmd on Windows
        os.environ["PATH"] = empty
        shutil.which = lambda name, *a, **k: str(v / "fakebin" / "claude") if name == "claude" else None
        try:
            out, err = en.call_claude("x", "m"), ""
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        finally:
            os.environ["PATH"], shutil.which = old_path, old_which
            os.environ.pop("FAKE_LOG", None)
            shutil.rmtree(empty)
        c.ok(out == "[]", "15. claude is found through shutil.which", err)
    guard(c, "15. claude is found through shutil.which", t15)

    # 16. carriage return: unchanged note not rewritten
    def t16():
        v = make_vault()
        ic = load(v, "import_claude")
        folder = v / CONV
        body = "pasted log\r\nline two\rline three"
        ic.write_note(folder, "cr (s2).md", {"type": "x"}, body)
        p = folder / "cr (s2).md"
        os.utime(p, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        ic.write_note(folder, "cr (s2).md", {"type": "x"}, body)
        c.ok(p.stat().st_mtime_ns == 1_000_000_000_000_000_000,
             "16. a note with a carriage return is not rewritten when unchanged")
    guard(c, "16. a note with a carriage return is not rewritten when unchanged", t16)

    # 17. local dates
    def t17():
        v = make_vault()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import import_claude as ic; "
                "print(ic.iso_to_date('2026-09-30T23:30:00Z'), '|', ic.iso_to_dt('2026-09-30T23:30:00Z'))")
        r = subprocess.run([sys.executable, "-c", code, str(v / "tools")], capture_output=True, text=True,
                           env={**os.environ, "TZ": "Asia/Singapore"})
        c.ok(r.stdout.strip() == "2026-10-01 | 2026-10-01 07:30", "17. UTC timestamps are shown in local time",
             r.stdout + r.stderr)
    guard(c, "17. UTC timestamps are shown in local time", t17)

    # 18. CRLF notes from the box: left alone, and an old checkpoint still matches them
    def t18():
        v = make_vault()
        ic = load(v, "import_claude")
        folder = v / CONV
        name = "2026-09-01 talk (aaaaaaaa).md"
        ic.write_note(folder, name, {"type": "claude-conversation"}, "# talk\n\nhello there\nsecond line")
        src = folder / name
        lf = src.read_bytes()
        src.write_bytes(lf.replace(b"\n", b"\r\n"))        # what write_text produced on Windows
        os.utime(src, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        ic.write_note(folder, name, {"type": "claude-conversation"}, "# talk\n\nhello there\nsecond line")
        untouched = src.stat().st_mtime_ns == 1_000_000_000_000_000_000 and b"\r\n" in src.read_bytes()
        crlf_size = src.stat().st_size
        # The pre-upgrade checkpoints recorded the CRLF size; the file is now LF (a checkout that
        # renormalises line endings), same text, new mtime.
        src.write_bytes(lf)
        key = str(src.relative_to(v))
        for state in (".extract_state.json", ".extract_skills_state.json"):
            write(v, f"tools/{state}", json.dumps({key: {"sig": f"123:{crlf_size}"}}))
        env = fake_env(v, FAKE_MODE="grow")
        rs = [run(v, s, env=env) for s in ("extract_notes.py", "extract_skills.py")]
        n = len(calls(v))
        c.ok(untouched and n == 0 and all(r.returncode == 0 for r in rs),
             "18. a CRLF note is not rewritten, and an old checkpoint matches it across CRLF/LF",
             f"untouched={untouched} calls={n} " + " | ".join(r.stdout[-150:] for r in rs))
    guard(c, "18. a CRLF note is not rewritten, and an old checkpoint matches it across CRLF/LF", t18)

    # 19. machine A re-extracts a grown conversation and gets nothing new; machine B must not pay
    def t19():
        ok, detail = True, []
        for script, state, kind in (("extract_notes.py", ".extract_state.json", "notes"),
                                    ("extract_skills.py", ".extract_skills_state.json", "skills")):
            v = make_vault()
            src = conv(v)
            env_a = fake_env(v, FAKE_MODE="grow", FAKE_KIND=kind, PALIMPSEST_MACHINE="alpha")
            run(v, script, env=env_a)
            with src.open("a") as fh:
                fh.write("\n---\n\nand a later turn\n")
            run(v, script, env=env_a)                         # ALREADY CAPTURED -> [] : no new note
            before = len(calls(v))
            (v / "tools" / state).unlink()                   # machine B: its own (empty) checkpoint
            os.utime(src, (1_000_000_000, 1_000_000_000))
            r = run(v, script, env=fake_env(v, FAKE_MODE="grow", FAKE_KIND=kind, PALIMPSEST_MACHINE="beta"))
            n = len(calls(v)) - before
            ok &= before == 2 and n == 0 and r.returncode == 0
            detail.append(f"{script}: A made {before} call(s), B made {n} {r.stdout[-150:]}")
        c.ok(ok, "19. a grown conversation that yielded nothing is not re-sent on the other machine",
             " | ".join(detail))
    guard(c, "19. a grown conversation that yielded nothing is not re-sent on the other machine", t19)

    # 20. lone surrogates from the model
    def t20():
        v = make_vault()
        conv(v)
        rs = []
        for script, reply, d in (
                ("extract_notes.py", '[{"title": "Cut \\ud83d emoji title", "body": "Body \\ud83d here."}]',
                 "10 Notes"),
                ("extract_skills.py", '[{"name": "Cut \\ud83d emoji skill", "steps": "do \\ud83d it", '
                                      '"when_to_use": "when \\ud83d", "why": "z"}]', "Skills/_proposed")):
            r = run(v, script, env=fake_env(v, FAKE_REPLY=reply))
            rs.append((script, r.returncode, [p.name for p in notes(v, d)], r.stdout[-200:]))
        c.ok(all(rc == 0 and len(got) == 1 and "emoji" in got[0] for _, rc, got, _ in rs),
             "20. a lone surrogate in a model field is written, not a failure every run", str(rs))
    guard(c, "20. a lone surrogate in a model field is written, not a failure every run", t20)

    for v in VAULTS:
        shutil.rmtree(v, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
