"""Regressions for the 2026-09-30 review, setup/config/launcher group.

1. [P1] setup.py offered `origin` as the push_remote default, so pressing Enter published the
   vault to whatever the clone came from — the harness repo, or the user's public fork.
2. A palimpsest.json with a syntax error (or a UTF-8 BOM) silently loaded as pure defaults,
   turning pull/push off while everything reported clean; re-running setup then overwrote it.
   The first fix raised from load() for every caller, which killed sync.py at import (no status
   file, no log) and left the session opener with no output — so the callers that report status
   get defaults plus a warning and config.problem(), and only setup.py loads strictly.
3. The printed cron line had an unquoted `cd` (a vault path with a space never synced), and the
   Windows task passed the script path unquoted to python.exe.
4. A sync time not in HH:MM form was saved and then crashed setup before it printed the hooks
   and the commit-guard instruction.
5. With core.autocrlf=true the pre-commit hook and the launcher were checked out with CRLF,
   which breaks /bin/sh and so blocks every commit. The eol rule only reaches fresh checkouts,
   so setup.py (and `setup.py --fix-line-endings`) repairs one that already has CRLF.
6. claude-code.sh was committed 100644, so `./claude-code.sh` failed on every fresh clone.

Set PALIMPSEST_REF to test another committed ref for 5 and 6 (default: the current branch).
"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from _util import Checks, REPO, TOOLS_SRC, git, make_vault as _make_vault, write

MADE: list[Path] = []


def make_vault() -> Path:
    MADE.append(_make_vault())
    return MADE[-1]


def vault_at(name: str) -> Path:
    """A vault whose root directory is called `name` (make_vault's name has no spaces)."""
    v = Path(tempfile.mkdtemp(prefix="palimpsest-test-")) / name
    shutil.copytree(TOOLS_SRC, v / "tools", ignore=shutil.ignore_patterns("cache", "__pycache__", "*.pyc"))
    git(v, "init", "-q", "-b", "main")
    return v


def setup(v: Path, answers: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(v / "tools" / "setup.py")], cwd=v, capture_output=True,
                          text=True, input="".join(a + "\n" for a in answers))


def hint(v: Path, at: str, system: str = "") -> str:
    code = ("import sys, platform; sys.path.insert(0, sys.argv[1]); "
            + (f"platform.system = lambda: {system!r}; " if system else "")
            + "import setup; print(setup.scheduler_hint(sys.argv[2]))")
    return subprocess.run([sys.executable, "-c", code, str(v / "tools"), at], capture_output=True,
                          text=True).stdout


def ps_literal(text: str, flag: str) -> str | None:
    """The value of `-Flag '<single-quoted PowerShell literal>'`, unescaped ('' -> ')."""
    m = re.search(rf"-{flag} '((?:[^']|'')*)'", text)
    return m.group(1).replace("''", "'") if m else None


def main() -> int:
    c = Checks("review 2026-09-30 — setup/config/launchers")
    ref = os.environ.get("PALIMPSEST_REF") or git(REPO, "rev-parse", "HEAD").stdout.strip()

    # 1. push_remote is never defaulted to origin
    v = make_vault()
    git(v, "remote", "add", "origin", "https://example.invalid/palimpsest.git")
    # deny list: n · model, skills, time: Enter · push: y · push_remote: Enter
    r = first = setup(v, ["n", "", "", "", "y", ""])
    cfg = json.loads((v / "palimpsest.json").read_text()) if (v / "palimpsest.json").exists() else {}
    c.ok(cfg.get("push_remote") != "origin" and not cfg.get("steps", {}).get("push"),
         "Enter at push_remote does not save origin (push stays off)",
         f"push_remote={cfg.get('push_remote')!r} push={cfg.get('steps', {}).get('push')!r}\n{r.stdout[-300:]}")
    v2 = make_vault()
    git(v2, "remote", "add", "origin", "https://example.invalid/palimpsest.git")
    r = setup(v2, ["n", "", "", "", "y", "origin", ""])     # typed origin, Enter at the confirmation
    cfg = json.loads((v2 / "palimpsest.json").read_text()) if (v2 / "palimpsest.json").exists() else {}
    c.ok(cfg.get("push_remote") != "origin" and not cfg.get("steps", {}).get("push"),
         "typing origin still needs an explicit yes before it is saved",
         f"push_remote={cfg.get('push_remote')!r}\n{r.stdout[-300:]}")

    # 2. an unreadable palimpsest.json is an error, not a silent reset to defaults
    v = make_vault()
    bad = '{"steps": {"pull": true, "push": true},\n "push_remote": "backup",}\n'
    write(v, "palimpsest.json", bad)
    probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); import config\n"
             "try:\n    print(json.dumps(config.load(strict=True)))\n"
             "except ValueError as e:\n    print('ERROR', e)")
    out = subprocess.run([sys.executable, "-c", probe, str(v / "tools")], capture_output=True, text=True).stdout
    c.ok(out.startswith("ERROR") and "palimpsest.json" in out,
         "a trailing comma in palimpsest.json raises from a strict load", out[:200])
    soft = ("import sys, json; sys.path.insert(0, sys.argv[1]); import config\n"
            "cfg = config.load(); config.load()\n"
            "print(json.dumps({'pull': cfg['steps']['pull'], 'problem': config.problem()}))")
    r = subprocess.run([sys.executable, "-c", soft, str(v / "tools")], capture_output=True, text=True)
    got = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    c.ok(r.returncode == 0 and got.get("pull") is False and "palimpsest.json" in got.get("problem", "")
         and r.stderr.count("palimpsest.json") == 1,
         "the default load degrades to defaults but warns once on stderr and exposes problem()",
         f"exit={r.returncode} stdout={r.stdout[:200]!r} stderr={r.stderr[:300]!r}")
    # the callers that report status must survive it: the opener still opens, the sync still
    # writes its status file and log (their steps stubbed — no pipeline work, no model)
    for script in ("briefing.py", "maintenance.py", "import_claude.py", "extract_notes.py",
                   "link_notes.py", "dedupe.py", "triage_skills.py", "weekly_review.py", "embed.py"):
        write(v, f"tools/{script}", "print('stub')\n")
    env = {k: val for k, val in os.environ.items() if k != "CLAUDE_BRAIN_NO_HOOK"}
    r = subprocess.run([sys.executable, str(v / "tools" / "hook_session_start.py")], cwd=v,
                       capture_output=True, text=True, env=env, timeout=120)
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        ctx = ""
    c.ok(r.returncode == 0 and "Brain session" in ctx,
         "the session opener still opens with an unparseable palimpsest.json",
         f"exit={r.returncode}\n{(r.stdout + r.stderr)[-400:]}")
    r = subprocess.run([sys.executable, str(v / "tools" / "sync.py")], cwd=v, capture_output=True,
                       text=True, timeout=300)
    c.ok((v / "tools" / ".sync_status.json").exists() and (v / "tools" / "sync.log").exists(),
         "sync.py still runs and writes .sync_status.json and sync.log with an unparseable config",
         f"exit={r.returncode}\n{(r.stdout + r.stderr)[-400:]}")
    r = setup(v, ["n", "", "", "", "n"])
    c.ok(r.returncode != 0 and (v / "palimpsest.json").read_text() == bad,
         "setup.py refuses to overwrite an unparseable palimpsest.json", (r.stdout + r.stderr)[-300:])
    (v / "palimpsest.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"push_remote": "backup"}).encode())
    out = subprocess.run([sys.executable, "-c", probe, str(v / "tools")], capture_output=True, text=True).stdout
    c.ok(out.startswith("{") and json.loads(out).get("push_remote") == "backup",
         "a palimpsest.json saved with a BOM (Notepad) loads", out[:200])

    # 3. the scheduler command works from a vault path with a space (and a %, special to cron)
    if os.name != "nt":
        v = vault_at("My Vault 50%")
        write(v, "tools/sync.py", "from pathlib import Path\n"
                                  "Path(__file__).with_name('RAN').write_text('ok')\n")
        line = next((l for l in hint(v, "06:00").splitlines() if "* * *" in l), "")
        cmd = line.split("* * * ", 1)[-1]
        bare_pct = re.search(r"(?<!\\)%", cmd)
        # cron turns an unescaped % into a newline and drops the backslash of \% before sh runs it
        subprocess.run(["sh", "-c", cmd.replace("\\%", "%")], cwd=tempfile.gettempdir(), capture_output=True)
        c.ok(not bare_pct and (v / "tools" / "RAN").exists(),
             "the printed cron line runs sync.py from a vault path with a space and a %", line)
        shutil.rmtree(v.parent)
    w = vault_at("John Smith's Vault")
    out = hint(w, "06:00", "Windows")
    arg, exe, wd = (ps_literal(out, f) for f in ("Argument", "Execute", "WorkingDirectory"))
    script = str(w.resolve() / "tools" / "sync.py")
    c.ok(arg == f'"{script}"' and exe == sys.executable and wd == str(w.resolve()),
         "the Windows task quotes the script path for python.exe and PowerShell",
         f"Argument={arg!r} Execute={exe!r} WorkingDirectory={wd!r}")
    shutil.rmtree(w.parent)

    # 1b. a saved push to origin is not dropped without saying so, and the prompts read cleanly
    v = make_vault()
    git(v, "remote", "add", "origin", "https://example.invalid/palimpsest.git")
    write(v, "palimpsest.json", json.dumps({"push_remote": "origin", "steps": {"push": True}}))
    r = setup(v, ["n", "", "", "", "", "", ""])     # Enter throughout, confirmation included
    cfg = json.loads((v / "palimpsest.json").read_text())
    c.ok(not cfg["steps"]["push"] and "turned OFF" in r.stdout and "example.invalid" in r.stdout,
         "re-running setup over push_remote=origin shows origin's URL and says push was turned off",
         r.stdout[-400:])
    c.ok("push_remote" in first.stdout and "[]" not in first.stdout,
         "an empty push_remote default renders no empty brackets",
         next((l for l in first.stdout.splitlines() if "[]" in l), first.stdout[-200:]))

    # 4. a malformed sync time is re-asked, and setup finishes
    v = make_vault()
    r = setup(v, ["n", "", "", "6am", "06:30", "n"])
    cfg = json.loads((v / "palimpsest.json").read_text()) if (v / "palimpsest.json").exists() else {}
    c.ok(r.returncode == 0 and cfg.get("sync", {}).get("at") == "06:30" and "core.hooksPath" in r.stdout,
         "a sync time not in HH:MM form is re-asked instead of crashing setup",
         f"at={cfg.get('sync', {}).get('at')!r}\n{(r.stdout + r.stderr)[-300:]}")

    # 5 + 6. a fresh clone: LF shell scripts under core.autocrlf=true, and an executable launcher
    d = Path(tempfile.mkdtemp(prefix="palimpsest-clone-"))
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(REPO), str(d)], check=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=d, check=True)
    subprocess.run(["git", "checkout", "-q", ref], cwd=d, check=True, capture_output=True)
    crlf = [p for p in ("tools/githooks/pre-commit", "claude-code.sh") if b"\r" in (d / p).read_bytes()]
    c.ok(not crlf, "shell scripts are checked out with LF under core.autocrlf=true", f"CRLF in {crlf}")
    # an existing checkout keeps its CRLF hook after pulling the eol rule; setup repairs it.
    # Reproduced the way it happens: check out the last commit before the rule, then the ref.
    rule = [h for h in git(d, "rev-list", ref, "--", ".gitattributes").stdout.split()
            if "eol=lf" in git(d, "show", f"{h}:.gitattributes").stdout
            and "eol=lf" not in git(d, "show", f"{h}^:.gitattributes", check=False).stdout]
    (d / "tools/githooks/pre-commit").unlink()      # else the unchanged blob is not rewritten
    before = f"{rule[-1]}^" if rule else ref     # a ref without the rule is its own "before"
    subprocess.run(["git", "checkout", "-q", "-f", before], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-q", ref], cwd=d, check=True, capture_output=True)
    stale = b"\r\n" in (d / "tools/githooks/pre-commit").read_bytes()
    dirty = git(d, "status", "--short").stdout
    r = subprocess.run([sys.executable, str(d / "tools" / "setup.py"), "--fix-line-endings"], cwd=d,
                       capture_output=True, text=True, input="")
    hook = d / "tools/githooks/pre-commit"
    c.ok(stale and not dirty and b"\r" not in hook.read_bytes() and not git(d, "status", "--short").stdout
         and (os.name == "nt" or os.access(hook, os.X_OK)),
         "setup.py --fix-line-endings rewrites a CRLF hook left by an older checkout (tree stays clean)",
         f"CRLF before={stale} status before={dirty!r}\n{(r.stdout + r.stderr)[-300:]}")
    shutil.rmtree(d)
    mode = git(REPO, "ls-tree", ref, "claude-code.sh").stdout.split()[:1]
    c.ok(mode == ["100755"], f"claude-code.sh is committed executable on {ref[:12]}", str(mode))
    for v in MADE:
        shutil.rmtree(v, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
