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
import json
from pathlib import Path
from datetime import datetime

TOOLS = Path(__file__).resolve().parent
VAULT = TOOLS.parent
CONFIG = VAULT / "palimpsest.json"

DEFAULTS = {
    "version": 1,
    "extraction_model": "claude-haiku-4-5-20251001",
    "steps": {
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
        "weekly": True,
        "briefing": True,
    },
    "timeouts": {"import": 180, "extract": 2400, "skills": 2400, "link": 120,
                 "maintenance": 120, "dedupe": 120, "weekly": 120, "briefing": 120},
    "sync": {"cadence": "daily", "at": "06:00"},
}


def load() -> dict:
    """Config merged over defaults. A missing or unreadable file yields pure defaults."""
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG.exists():
        try:
            user = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return cfg
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save(cfg: dict) -> None:
    cfg["configured_at"] = datetime.now().isoformat(timespec="seconds")
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def is_configured() -> bool:
    return CONFIG.exists()
