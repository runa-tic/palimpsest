"""Regressions for the 2026-09-30 review, setup/config/launcher group.

1. [P1] setup.py offered `origin` as the push_remote default, so pressing Enter published the
   vault to whatever the clone came from — the harness repo, or the user's public fork.
2. A palimpsest.json with a syntax error (or a UTF-8 BOM) silently loaded as pure defaults,
   turning pull/push off while everything reported clean; re-running setup then overwrote it.
3. The printed cron line had an unquoted `cd` (a vault path with a space never synced), and the
   Windows task passed the script path unquoted to python.exe.
4. A sync time not in HH:MM form was saved and then crashed setup before it printed the hooks
   and the commit-guard instruction.
5. With core.autocrlf=true the pre-commit hook and the launcher were checked out with CRLF,
   which breaks /bin/sh and so blocks every commit.
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
    r = setup(v, ["n", "", "", "", "y", ""])
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
             "try:\n    print(json.dumps(config.load()))\n"
             "except ValueError as e:\n    print('ERROR', e)")
    out = subprocess.run([sys.executable, "-c", probe, str(v / "tools")], capture_output=True, text=True).stdout
    c.ok(out.startswith("ERROR") and "palimpsest.json" in out,
         "a trailing comma in palimpsest.json raises instead of loading defaults", out[:200])
    r = setup(v, ["n", "", "", "", "n"])
    c.ok(r.returncode != 0 and (v / "palimpsest.json").read_text() == bad,
         "setup.py refuses to overwrite an unparseable palimpsest.json", (r.stdout + r.stderr)[-300:])
    (v / "palimpsest.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"push_remote": "backup"}).encode())
    out = subprocess.run([sys.executable, "-c", probe, str(v / "tools")], capture_output=True, text=True).stdout
    c.ok(not out.startswith("ERROR") and json.loads(out).get("push_remote") == "backup",
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
    shutil.rmtree(d)
    mode = git(REPO, "ls-tree", ref, "claude-code.sh").stdout.split()[:1]
    c.ok(mode == ["100755"], f"claude-code.sh is committed executable on {ref[:12]}", str(mode))
    for v in MADE:
        shutil.rmtree(v, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
