#!/usr/bin/env python3
"""First-run setup: make the five decisions that shouldn't be inherited silently.

Run it directly (`python tools/setup.py`) for the interactive version, or let the
SessionStart hook offer to walk you through the same choices conversationally — the hook
detects an unconfigured vault and hands the agent this file's questions.

Non-interactive invocations (cron, CI, a piped shell) print the plan and change nothing,
because a setup script that blocks on stdin in a scheduled job is a wedged job. The one
exception is a repair, not a choice: tracked files .gitattributes pins to LF are rewritten
with LF if the checkout has them in CRLF (see fix_line_endings).

  python tools/setup.py --fix-line-endings   only that repair — for a Windows vault cloned with
                                             core.autocrlf=true before the eol rules existed
"""
from __future__ import annotations
import sys, os, re, json, platform, shlex, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfgmod

MODELS = [
    ("claude-haiku-4-5-20251001", "cheapest and fastest; fine for distilling transcripts"),
    ("claude-sonnet-5", "noticeably better notes, several times the cost per night"),
]


HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class NotATerminal(Exception):
    """stdin hit EOF, so nobody is answering. Never treat that as 'accept all defaults'."""


def ask(prompt: str, default: str, options: list[str] | None = None) -> str:
    # An empty default gets no brackets: "push_remote []" read like a broken prompt.
    hint = (f" ({'/'.join(options)})" if options else "") + (f" [{default}]" if default else "")
    try:
        got = input(f"{prompt}{hint}: ").strip()
    except EOFError:
        # isatty() lies under some shells (MSYS/Git Bash reports a tty for redirected stdin),
        # so EOF is the reliable signal. Bail out rather than silently choosing for the user.
        raise NotATerminal
    if not got:
        return default
    if options and got not in options:
        print(f"  not one of {options}; keeping {default}")
        return default
    return got


def yes(prompt: str, default: bool) -> bool:
    return ask(prompt, "y" if default else "n", ["y", "n"]) == "y"


def scheduler_hint(at: str) -> str:
    """The exact command for this OS. A cadence you can't install is a cadence you won't."""
    sync = Path(__file__).resolve().parent / "sync.py"
    hh, mm = at.split(":")
    if platform.system() == "Windows":
        # A single-quoted PowerShell literal escapes ' as ''. -Argument reaches python.exe as a
        # raw command line, so the path needs its own double quotes or it splits at a space.
        ps = lambda s: "'" + str(s).replace("'", "''") + "'"
        arg = f'"{sync}"'
        return (
            "Windows — run this in an ELEVATED PowerShell (Task Scheduler needs admin).\n"
            "Use the cmdlets, not schtasks: PowerShell 5.1 mangles nested quotes in /TR.\n\n"
            f"  $act = New-ScheduledTaskAction -Execute {ps(sys.executable)} "
            f"-Argument {ps(arg)} -WorkingDirectory {ps(cfgmod.VAULT)}\n"
            f"  $trg = New-ScheduledTaskTrigger -Daily -At {hh}:{mm}\n"
            "  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable\n"
            "  Register-ScheduledTask -TaskName 'Palimpsest Sync' -Action $act "
            "-Trigger $trg -Settings $set -Force\n\n"
            "Do NOT settle for a Startup-folder shortcut: it fires at logon only, so on a\n"
            "machine that stays up for weeks the vault silently stops syncing."
        )
    # Quoted for sh: an unquoted `cd` into "My Vault" failed, && skipped the sync, and the
    # redirect hid it — every night. cron also turns a bare % into a newline, so escape it.
    # stdout only repeats what sync.py already writes to sync.log; stderr is what it cannot log
    # itself (a crash at import, config.py's DEFAULTS warning), so it is appended there, not lost.
    q = lambda s: shlex.quote(str(s)).replace("%", "\\%")
    return ("macOS / Linux — add to `crontab -e`:\n\n"
            f"  {int(mm)} {int(hh)} * * * cd {q(cfgmod.VAULT)} && {q(sys.executable)} {q(sync)} "
            f">/dev/null 2>>{q(sync.with_name('sync.log'))}")


HOOKS_JSON = r"""Register the hooks. Merge this into `.claude/settings.local.json` in the vault
root (or settings.json if you want it shared). Note the double nesting — event names live
under a top-level "hooks" key, and each event holds a list of matchers that each hold a list
of hooks. Dropping the outer "hooks" wrapper is silent: Claude Code reads the file, finds no
hooks, and nothing ever fires. $CLAUDE_PROJECT_DIR keeps the paths portable.

{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command",
                  "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/hook_session_start.py\""}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command",
                  "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/hook_record.py\""}]}
    ]
  }
}

Verify with `/hooks` in a new session: if the list is empty, the shape is wrong."""


def plan(cfg: dict) -> str:
    steps_on = [k for k, v in cfg["steps"].items() if v]
    return (f"  extraction model : {cfg['extraction_model']}\n"
            f"  pipeline steps   : {', '.join(steps_on)}\n"
            f"  skills proposer  : {'ON' if cfg['steps']['skills'] else 'off (recommended)'}\n"
            f"  sync cadence     : {cfg['sync']['cadence']} at {cfg['sync']['at']}")


def fix_line_endings() -> list[str]:
    """Rewrite with LF every tracked file that .gitattributes pins to eol=lf but that sits in the
    working tree with CRLF. The eol rule only reaches fresh checkouts: a core.autocrlf=true vault
    that got its CRLF pre-commit before the rule landed keeps it — the blob did not change, so git
    never rewrites the file and `git status` is clean — and '#!/bin/sh\\r' refuses every commit,
    the nightly backup's too. Only the line endings change, so local edits survive."""
    try:
        p = subprocess.run(["git", "ls-files", "--eol", "-z"], cwd=str(cfgmod.VAULT),
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    fixed = []
    for rec in p.stdout.split("\0") if p.returncode == 0 else []:
        info, _, path = rec.partition("\t")
        f = info.split()
        # Only where the blob itself is LF: then LF bytes are exactly what git stores.
        if not ("eol=lf" in f and "i/lf" in f and ("w/crlf" in f or "w/mixed" in f)):
            continue
        target = cfgmod.VAULT / path
        try:
            data = target.read_bytes()
            target.write_bytes(data.replace(b"\r\n", b"\n"))   # in place, so the file mode survives
        except OSError:
            continue
        fixed.append(path)
        # The index still holds the CRLF file's size, and git counts a size change as modified
        # without reading the content, so the tree would show ' M' with an empty diff. When the
        # content is exactly the staged blob, re-staging it only refreshes that stat.
        git = lambda *a: subprocess.run(["git", *a], cwd=str(cfgmod.VAULT), capture_output=True,
                                        text=True).stdout.strip()
        if git("hash-object", "--", path) == git("rev-parse", f":{path}"):
            git("update-index", "-q", "--", path)
    return fixed


def main() -> int:
    for path in fix_line_endings():
        print(f"setup: {path} had CRLF line endings; rewrote it with LF")
    if "--fix-line-endings" in sys.argv[1:]:
        return 0
    try:
        cfg = cfgmod.load(strict=True)
    except cfgmod.ConfigError as e:
        # Never interview over it: save() would replace the user's push_remote, machine and
        # probes with defaults plus five answers.
        print(f"setup: {e}\nNothing was written.", file=sys.stderr)
        return 1
    print("\nPalimpsest setup\n" + "=" * 60)
    print(f"vault: {cfgmod.VAULT}\n")
    try:
        return interview(cfg)
    except NotATerminal:
        print("\n\nNo terminal on stdin — nothing was written. Current effective settings:\n")
        print(plan(cfg))
        print(f"\nRun `python tools/setup.py` from a real terminal to choose, or write "
              f"{cfgmod.CONFIG.name} yourself (schema in tools/config.py).")
        return 0


def interview(cfg: dict) -> int:
    print("Five decisions. Enter accepts the default in brackets.\n")

    # FIRST, deliberately. Redaction is applied at WRITE time against whatever the deny list
    # holds then, and the Stop hook records the session after every turn — so asking this last
    # means several turns are already on disk unprotected, and a later entry cannot reach back
    # and scrub them. Reproducing this project's own documented leak during its setup would be
    # a poor advertisement for reading the notes.
    print("1) Redaction deny list — FIRST, deliberately. tools/.redact_terms.txt is scrubbed")
    print("   from recorded transcripts at WRITE time and blocked at commit time. Anything")
    print("   said before this file exists is already on disk, and a later entry cannot")
    print("   reach back and scrub it. Deny-list-only: anything unlisted passes verbatim.")
    deny = cfgmod.TOOLS / ".redact_terms.txt"
    if not deny.exists() and yes("   create it from the example now", True):
        deny.write_text((cfgmod.TOOLS / ".redact_terms.example.txt").read_text(encoding="utf-8"),
                        encoding="utf-8")
        print(f"   wrote {deny.name} — add your addresses and phone numbers NOW, before the")
        print("   rest of this setup is recorded")

    print("\n2) Which model distils transcripts into notes? It runs once per conversation,")
    print("   every night, so this is the recurring cost of the whole system.")
    for m, why in MODELS:
        print(f"     {m} — {why}")
    cfg["extraction_model"] = ask("   model", cfg["extraction_model"])

    print("\n3) Run the skills proposer in the nightly pipeline?")
    print("   It mines sessions for reusable procedures into Skills/_proposed/ for you to")
    print("   promote by hand. It is off by default because in the vault this came from it")
    print("   burned most of the sync window, failed most of its inputs without checkpointing")
    print("   (so the same failures retried nightly), and grew a 654-deep proposal queue")
    print("   against 16 promoted skills. Better run by hand with --limit when you want it.")
    cfg["steps"]["skills"] = yes("   enable in nightly sync", cfg["steps"]["skills"])

    print("\n4) When should the sync run? It rewrites notes while it works, so pick an hour")
    print("   you are never mid-session in the vault.")
    # Checked here, not trusted: scheduler_hint() splits on ':' after the file is saved, so '6am'
    # used to crash setup before it printed the hooks and the commit-guard instruction.
    at = cfg["sync"]["at"] if HHMM.match(str(cfg["sync"]["at"])) else cfgmod.DEFAULTS["sync"]["at"]
    while not (m := HHMM.match(ask("   time (HH:MM, local)", at))):
        print("   not a 24-hour HH:MM time, e.g. 06:00 or 23:30")
    cfg["sync"]["at"] = f"{int(m[1]):02d}:{m[2]}"

    print("\n5) Nightly backup? The pipeline can end by committing that night's notes and")
    print("   pushing them to the remote named in push_remote, so the vault stops drifting")
    print("   from its backup. It never bypasses the commit guards, never force-pushes,")
    print("   never commits code. Leave it off until you have a remote you trust.")
    pushing = bool(cfg["steps"].get("push"))
    cfg["steps"]["push"] = yes("   enable nightly commit+push", pushing)
    if cfg["steps"]["push"]:
        # Never inferred: a clone of this repo has `origin` pointing at the harness, so an
        # unconfigured push would publish a private vault into someone else's project.
        print("   Which remote? NOT guessed — if you cloned Palimpsest, `origin` is the")
        print("   harness repo and your notes would be pushed there.")
        # No default either: a bracketed [origin] turned "Enter accepts the default" into
        # publishing the vault to the harness repo or the user's public fork. Blank = push off.
        was = (cfg.get("push_remote") or "") if pushing else ""
        remote = ask("   push_remote (blank leaves push off)", cfg.get("push_remote") or "")
        if remote == "origin":
            # Asked every time, even when origin was already saved: the setup that preceded this
            # one defaulted to origin, so a saved origin may be an Enter, not a decision.
            url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(cfgmod.VAULT),
                                 capture_output=True, text=True).stdout.strip() or "no URL"
            print(f"   `origin` is {url} — the repo this clone came from, unless you changed it.")
            if not yes("   push your notes THERE", False):
                remote = ""
        if not remote:
            print(f"   nightly push turned OFF (it was pushing to `{was}`)" if was else
                  "   no remote named — nightly push stays OFF")
            cfg["steps"]["push"] = False
        cfg["push_remote"] = remote or None

    cfgmod.save(cfg)
    print("\n" + "=" * 60)
    print(f"Saved {cfgmod.CONFIG.name}:\n")
    print(plan(cfg))
    print("\n" + "-" * 60)
    print(scheduler_hint(cfg["sync"]["at"]))
    print("\n" + "-" * 60)
    print(HOOKS_JSON)
    print("\nThen: git config core.hooksPath tools/githooks   (secret + PII commit guards)")
    print("And:  python tools/sync.py                        (first run, safe to repeat)\n")
    return 0


if __name__ == "__main__":
    # The house convention (state.py, scan_secrets.py): output is UTF-8 whatever the code page, so
    # a piped or logged run on a Windows ANSI code page neither crashes nor writes mojibake.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
