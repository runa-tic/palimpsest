#!/usr/bin/env python3
"""Secret guard: scan for credential-shaped strings before they reach git.

Why: the Stop hook auto-saves each Claude session into the vault verbatim, so
anything pasted into chat (an API key, a token) lands in a tracked note with no
review. This catches the high-confidence shapes before a commit/push.

Severity:
  HIGH  real credentials (API keys, bot tokens, private keys) -> blocks commit (exit 1)
  WARN  policy-sensitive but often public (EVM addresses, generic secret-ish
        assignments) -> reported only, never blocks (unless --strict)

Usage (from vault root):
  python tools/scan_secrets.py            # scan staged changes (used by pre-commit)
  python tools/scan_secrets.py --all      # scan the whole vault
  python tools/scan_secrets.py PATH ...   # scan specific files/dirs
  python tools/scan_secrets.py --strict   # also block on WARN findings

Allowlist: tools/.secret_scan_allow.txt  (substrings of known-public values;
           any line containing one is exempt; '#' starts a comment).
Bypass one commit (use sparingly): git commit --no-verify
"""
from __future__ import annotations
import re, sys, subprocess
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parent     # was VAULT / "_tools": the allow list was never read
VAULT = TOOLS.parent
ALLOW_FILE = TOOLS / ".secret_scan_allow.txt"

# Directories never scanned (protected, binary-heavy, or would self-flag).
SKIP_DIRS = {".git", "_media", "node_modules", ".obsidian", "__pycache__"}
# Specific repo-relative paths never scanned.
SKIP_PATHS = {
    "tools/.secrets",
    "tools/scan_secrets.py",
    "tools/.secret_scan_allow.txt",
    "tools/.redact_terms.txt",
}

# (label, compiled regex). These shapes are high-confidence credentials.
HIGH = [
    ("CoinGecko API key",   re.compile(r"CG-[A-Za-z0-9]{20,}")),
    # Current keys (sk-proj-, sk-svcacct-, sk-admin-) have a base64url body with '_' and '-', so
    # the alphanumeric-only class missed about half of them and cut the rest short, leaving the
    # tail in the note (review, 2026-09-30). The lookbehind keeps prose like "risk-admin-..." out.
    ("OpenAI key",          re.compile(r"(?<![A-Za-z0-9])sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
                                       r"|sk-[A-Za-z0-9]{20,}")),
    ("Anthropic key",       re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token",        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}")),
    ("Google API key",      re.compile(r"AIza[A-Za-z0-9_-]{30,}")),
    ("Slack token",         re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key id",   re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    # The secret half has no prefix, only its label. Without this rule redaction masked the id and
    # left a working secret key, which also removed the one thing the guard would have blocked on.
    ("AWS secret access key", re.compile(
        r"(?i)(?:aws_secret_access_key|secretaccesskey)['\"\s:=]+[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    # Lookarounds, not \b: in a Bot API URL (.../bot<token>/getMe) "bot" runs straight into the
    # digits, and a secret may end in '-'; \b missed both.
    ("Telegram bot token",  re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("Private key block",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Telegram api_hash",   re.compile(r"api_hash['\"\s:=]+[a-f0-9]{32}\b", re.I)),
]
# Lower-confidence / often-public shapes: report but don't block by default.
WARN = [
    ("EVM address (wallet or contract)", re.compile(r"\b0x[a-fA-F0-9]{40}\b")),
    ("Secret-ish assignment", re.compile(
        r"(?i)(secret|token|passwd|password|api[_-]?key)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
]


def load_allow() -> list[str]:
    if not ALLOW_FILE.exists():
        return []
    out = []
    for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def mask(s: str) -> str:
    if len(s) <= 10:
        return s[:3] + "…"
    return s[:6] + "…" + s[-3:]


def scan_text(text: str, allow: list[str]) -> list[tuple]:
    """Return list of (severity, label, lineno, masked) findings."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(a in line for a in allow):
            continue
        for sev, rules in (("HIGH", HIGH), ("WARN", WARN)):
            for label, rx in rules:
                m = rx.search(line)
                if m:
                    findings.append((sev, label, lineno, mask(m.group(0))))
    return findings


def staged_files() -> list[str]:
    """Paths whose staged version is new content: Added, Copied, Modified, and Renamed. With
    --name-only a rename or copy yields its DESTINATION, which is the path `git show :path`
    reads. R was missing until the Codex review of 2026-09: rename a note and add a key in the
    same commit and neither guard read a byte of it."""
    # Everything staged except deletions (lowercase d excludes). Listing types to include kept
    # missing one: ACM dropped renames, then ACMR dropped type changes (T) — a symlink replaced by
    # a file holding a key sailed through (external review, 2026-09-30).
    out = subprocess.run(
        ["git", "diff", "--cached", "-z", "--name-only", "--diff-filter=d"],
        cwd=VAULT, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return [p for p in out.split("\0") if p]


def staged_content(path: str) -> str | None:
    r = subprocess.run(["git", "show", f":{path}"], cwd=VAULT,
                        capture_output=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else None


def is_skipped(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(d in SKIP_DIRS for d in parts):
        return True
    return any(rel.replace("\\", "/").startswith(s) for s in SKIP_PATHS)


def read_file(p: Path) -> str | None:
    try:
        if p.stat().st_size > 5_000_000:
            return None
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def walk_all() -> list[tuple[str, str]]:
    items = []
    for p in VAULT.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(VAULT))
        if is_skipped(rel):
            continue
        txt = read_file(p)
        if txt is not None:
            items.append((rel, txt))
    return items


def main() -> int:
    args = [a for a in sys.argv[1:]]
    strict = "--strict" in args
    args = [a for a in args if a != "--strict"]

    allow = load_allow()
    sources: list[tuple[str, str]] = []

    if "--all" in args:
        sources = walk_all()
        mode = "whole vault"
    elif args:
        for a in args:
            p = Path(a)
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        fr = f.resolve()    # the check and the relative name must use the same path:
                        rel = str(fr.relative_to(VAULT)) if VAULT in fr.parents else str(fr)  # a relative dir arg crashed
                        if not is_skipped(rel):
                            t = read_file(f)
                            if t is not None:
                                sources.append((rel, t))
            elif p.is_file():
                t = read_file(p)
                if t is not None:
                    sources.append((str(p), t))
        mode = f"{len(sources)} path(s)"
    else:
        for rel in staged_files():
            if is_skipped(rel):
                continue
            # The name is content too: import_claude.py names files after the conversation.
            sources.append((f"{rel} [path]", rel))
            c = staged_content(rel)
            if c is not None:
                sources.append((rel, c))
        mode = "staged changes"

    high, warn = [], []
    for rel, text in sources:
        if rel.endswith(" [path]"):
            # The path is being scanned as content, so it must not be printed raw either.
            for _, rules in (("HIGH", HIGH), ("WARN", WARN)):
                for _, rx in rules:
                    rel = rx.sub(lambda m: mask(m.group(0)), rel)
        for sev, label, lineno, masked in scan_text(text, allow):
            (high if sev == "HIGH" else warn).append((rel, label, lineno, masked))

    if not high and not warn:
        print(f"secret-scan: clean ({mode}).")
        return 0

    if high:
        print(f"\nsecret-scan: {len(high)} HIGH finding(s) — likely real credentials:")
        for rel, label, lineno, masked in high:
            print(f"  [HIGH] {label:<22} {rel}:{lineno}  {masked}")
    if warn:
        print(f"\nsecret-scan: {len(warn)} WARN finding(s) — review (often public):")
        for rel, label, lineno, masked in warn:
            print(f"  [warn] {label:<22} {rel}:{lineno}  {masked}")

    blocking = bool(high) or (strict and bool(warn))
    if blocking:
        print("\nRedact the value, or if it is known-public add a substring to "
              f"{ALLOW_FILE.relative_to(VAULT)} .")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
