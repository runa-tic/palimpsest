#!/usr/bin/env python3
"""First-run setup: make the five decisions that shouldn't be inherited silently.

Run it directly (`python tools/setup.py`) for the interactive version, or let the
SessionStart hook offer to walk you through the same choices conversationally — the hook
detects an unconfigured vault and hands the agent this file's questions.

Non-interactive invocations (cron, CI, a piped shell) print the plan and change nothing,
because a setup script that blocks on stdin in a scheduled job is a wedged job.
"""
from __future__ import annotations
import sys, os, json, platform
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfgmod

MODELS = [
    ("claude-haiku-4-5-20251001", "cheapest and fastest; fine for distilling transcripts"),
    ("claude-sonnet-5", "noticeably better notes, several times the cost per night"),
]


class NotATerminal(Exception):
    """stdin hit EOF, so nobody is answering. Never treat that as 'accept all defaults'."""


def ask(prompt: str, default: str, options: list[str] | None = None) -> str:
    hint = f" [{default}]" if not options else f" ({'/'.join(options)}) [{default}]"
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
    launcher = f'"{sys.executable}" "{Path(__file__).resolve().parent / "sync.py"}"'
    if platform.system() == "Windows":
        hh, mm = at.split(":")
        return (
            "Windows — run this in an ELEVATED PowerShell (Task Scheduler needs admin).\n"
            "Use the cmdlets, not schtasks: PowerShell 5.1 mangles nested quotes in /TR.\n\n"
            f"  $act = New-ScheduledTaskAction -Execute '{sys.executable}' "
            f"-Argument '{Path(__file__).resolve().parent / 'sync.py'}'\n"
            f"  $trg = New-ScheduledTaskTrigger -Daily -At {hh}:{mm}\n"
            "  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable\n"
            "  Register-ScheduledTask -TaskName 'Palimpsest Sync' -Action $act "
            "-Trigger $trg -Settings $set -Force\n\n"
            "Do NOT settle for a Startup-folder shortcut: it fires at logon only, so on a\n"
            "machine that stays up for weeks the vault silently stops syncing."
        )
    hh, mm = at.split(":")
    return ("macOS / Linux — add to `crontab -e`:\n\n"
            f"  {int(mm)} {int(hh)} * * * cd {cfgmod.VAULT} && {launcher} >/dev/null 2>&1")


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


def main() -> int:
    cfg = cfgmod.load()
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

    print("1) Which model distils transcripts into notes? It runs once per conversation,")
    print("   every night, so this is the recurring cost of the whole system.")
    for m, why in MODELS:
        print(f"     {m} — {why}")
    cfg["extraction_model"] = ask("   model", cfg["extraction_model"])

    print("\n2) Run the skills proposer in the nightly pipeline?")
    print("   It mines sessions for reusable procedures into Skills/_proposed/ for you to")
    print("   promote by hand. It is off by default because in the vault this came from it")
    print("   burned most of the sync window, failed most of its inputs without checkpointing")
    print("   (so the same failures retried nightly), and grew a 654-deep proposal queue")
    print("   against 16 promoted skills. Better run by hand with --limit when you want it.")
    cfg["steps"]["skills"] = yes("   enable in nightly sync", cfg["steps"]["skills"])

    print("\n3) When should the sync run? It rewrites notes while it works, so pick an hour")
    print("   you are never mid-session in the vault.")
    cfg["sync"]["at"] = ask("   time (HH:MM, local)", cfg["sync"]["at"])

    print("\n4) Nightly backup? The pipeline can end by committing that night's notes and")
    print("   pushing them to your git remote, so the vault stops drifting from its backup.")
    print("   It never bypasses the commit guards, never force-pushes, never commits code.")
    print("   Leave it off until you have a remote you trust AND have seeded the deny list.")
    cfg["steps"]["push"] = yes("   enable nightly commit+push", cfg["steps"].get("push", False))

    print("\n5) Redaction deny list. Anything you put in tools/.redact_terms.txt is scrubbed")
    print("   from recorded transcripts and blocked at commit time. It is deny-list-only by")
    print("   design — blanket scrubbing shreds real content — which means anything you do")
    print("   not list passes through verbatim. Seed it now with your own addresses.")
    deny = cfgmod.TOOLS / ".redact_terms.txt"
    if not deny.exists() and yes("   create it from the example now", True):
        deny.write_text((cfgmod.TOOLS / ".redact_terms.example.txt").read_text(encoding="utf-8"),
                        encoding="utf-8")
        print(f"   wrote {deny.name} — edit it before your first commit")

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
    sys.exit(main())
