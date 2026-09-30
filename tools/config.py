#!/usr/bin/env python3
"""Palimpsest configuration — the handful of choices that are genuinely the user's.

Lives at the vault root as `palimpsest.json` so it sits with the content it governs, not
with the code. Everything here has a working default; the file exists so that a new user
makes four decisions deliberately instead of inheriting one person's habits:

  * which model distils notes (cost vs quality, per conversation, every night)
  * whether the skills proposer runs in the nightly pipeline (see DEFAULTS below)
  * when the sync runs, and therefore what it collides with
  * what never leaves the machine (the redaction deny list)

`is_configured()` is what the SessionStart hook uses to decide whether to greet a brand-new
vault with an onboarding brief instead of a briefing.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from datetime import datetime

TOOLS = Path(__file__).resolve().parent
VAULT = TOOLS.parent
CONFIG = VAULT / "palimpsest.json"
# `config.py --check` exits with this when palimpsest.json is unreadable (distinct from 1, which a
# Python crash also returns, and from 9009, a missing interpreter on Windows).
CHECK_BROKEN = 3

DEFAULTS = {
    "version": 1,
    "extraction_model": "claude-haiku-4-5-20251001",
    "steps": {
        # OFF by default: only meaningful with a remote (push_remote below). Rebases onto the remote
        # BEFORE anything else runs, so a second machine never renders today's daily on a tree the
        # other one has moved past; if it fails, the briefing step is skipped for that run.
        "pull": False,
        "import": True,
        "extract": True,
        # OFF by default, and not out of caution. In the vault this came from, the skills
        # proposer burned 32 of one sync's 34 minutes, failed 70 of 99 conversations without
        # writing signatures — so the same 70 retried every run — and grew a queue of 654
        # proposals against 16 actually-promoted skills. Turn it on deliberately, ideally
        # with --limit, once you want proposals more than you want a fast nightly run.
        "skills": False,
        "link": True,
        "maintenance": True,
        "dedupe": True,
        # Cheap (~1s) and it only decides reading order, so it stays on even when the
        # skills proposer above is off — there is already a queue to work through.
        "triage": True,
        "weekly": True,
        # Refreshes the local embedding index (tools/embed.py) over whatever the night changed.
        # A no-op until sentence-transformers is installed (see SETUP.md), so it is safe on by
        # default; with it, a normal day's edits take well under a minute.
        "embed": True,
        # The State ledger's read-only probes (tools/state.py probe): this machine's last sync
        # run, where the checkout stands against the remote, and any probes declared below.
        # OFF by default — it creates State/ in the vault; turn it on when you want the ledger.
        "state": False,
        "briefing": True,
        # OFF by default, deliberately. It commits and pushes your vault to a remote — a
        # sensible default for the author, an unpleasant surprise for anyone else. Turn it on
        # once you have a remote you trust and have seeded tools/.redact_terms.txt, since the
        # commit guards are what stand between an unattended commit and a published secret.
        "push": False,
    },
    "timeouts": {"pull": 200, "state": 120, "import": 180, "extract": 2400, "skills": 2400, "link": 120,
                 "maintenance": 120, "dedupe": 120, "triage": 120, "weekly": 120, "embed": 1800,
                 "briefing": 120, "push": 330},
    # pull/push run vault_push.py, whose worst case is LOCK_WAIT_S 45 + NET_TIMEOUT 120 per network
    # call (pull; push adds a second): a step timeout below that kills it inside its own cleanup.
    "sync": {"cadence": "daily", "at": "06:00"},
    # Where the nightly backup pushes. MUST be set explicitly, and the reason is sharp: if you
    # cloned Palimpsest and are using the clone as your vault, `origin` points at the HARNESS
    # repo — so an unconfigured push would commit your private notes into someone else's
    # project. Naming the remote is the one thing that cannot be safely defaulted.
    "push_remote": None,
    # This machine's name in the State ledger (tools/state.py); default: the short hostname. Two
    # machines sharing a vault must not share a name.
    "machine": None,
    # Command probes for the State ledger — anything machine-specific (a server, a service, a
    # timer) is declared here, never hard-coded. See tools/state.py's docstring for the format.
    "probes": [],
    # Hosts tried (TCP 443) before a failed probe is recorded as "unreachable": if none answers,
    # it is this machine that is offline, and nothing is recorded about the target.
    "network_control": ["github.com", "1.1.1.1"],
}


class ConfigError(ValueError):
    """palimpsest.json exists but cannot be read as a JSON object."""


_WARNED = False


def _read_user() -> dict:
    """The user's palimpsest.json as a dict ({} when there is none). Raises ConfigError."""
    if not CONFIG.exists():
        return {}
    try:
        # utf-8-sig: Windows Notepad saves with a BOM, which plain utf-8 json.loads rejects.
        user = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{CONFIG.name} is unreadable ({e}); fix it or move it aside and "
                          f"re-run tools/setup.py") from e
    if not isinstance(user, dict):
        raise ConfigError(f"{CONFIG.name} must hold a JSON object, not {type(user).__name__}")
    return user


def problem() -> str:
    """Why palimpsest.json cannot be used, or '' when it can (or does not exist)."""
    try:
        _read_user()
    except ConfigError as e:
        return str(e)
    return ""


def load(strict: bool = False) -> dict:
    """Config merged over defaults. A missing file yields pure defaults.

    An unreadable file is never quietly the same as a missing one. strict=True raises
    ConfigError — setup.py, which would otherwise save defaults plus five answers over the
    user's push_remote, machine and probes. The default degrades to DEFAULTS, because the callers
    are the nightly sync, the session opener, the ledger and the push, and a raise there killed
    sync.py at import (no .sync_status.json, no sync.log) and left the opener with no output.

    Degrading must not read as a clean night, though: pull, push and state default OFF, so a sync
    on DEFAULTS reports every step it ran as clean while the backup and the pull have silently
    stopped. A stderr warning alone does not reach anyone (cron discarded it, and a hook's stderr
    never reaches the session), so each caller asks problem() and says so where it reports:
    sync.py counts a `config` failure in its receipt and log, the opener prints a Config line,
    vault_push names the unreadable file instead of an unset push_remote."""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = _read_user()
    except ConfigError as e:
        if strict:
            raise
        _fell_back(str(e))
        return cfg
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _fell_back(why: str) -> None:
    """Once per process: warn on stderr."""
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    print(f"palimpsest: WARNING {why}. Running on DEFAULTS until it is fixed — pull, push "
          f"and state are OFF.", file=sys.stderr)


def save(cfg: dict) -> None:
    cfg["configured_at"] = datetime.now().isoformat(timespec="seconds")
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def is_configured() -> bool:
    return CONFIG.exists()


if __name__ == "__main__":
    # `python tools/config.py --check` — the launchers' pre-flight. Exit CHECK_BROKEN (3) with the
    # problem on stdout when palimpsest.json is unreadable, 0 otherwise.
    why = problem()
    if why:
        print(f"palimpsest: WARNING {why}.\n  Until it is fixed the sync and the session opener "
              f"run on DEFAULTS — pull, push and state are OFF.")
        sys.exit(CHECK_BROKEN)
