#!/usr/bin/env python3
"""Mine saved Claude conversations for REUSABLE PROCEDURES and propose them as skills.

The procedural counterpart to extract_notes.py. Where extract_notes pulls out durable
*knowledge* (facts you recall), this pulls out durable *procedures* (playbooks you act
on): "when X, do Y, because Z" — repeatable techniques, gotchas-with-a-fix, operational
recipes. Mimics a Hermes-style agent writing a reusable skill doc after solving a hard
problem.

SAFETY: this only ever PROPOSES. Candidates are written to "Skills/_proposed/" with
status: proposed. A human (or Claude) reviews, edits, and promotes the good ones into
"Skills/" — nothing is auto-published. See Skills/README.md.

Usage (from vault root):
  python _tools/extract_skills.py                 # process new/changed conversations only
  python _tools/extract_skills.py --force         # reprocess everything
  python _tools/extract_skills.py --dry-run       # show what would be proposed
  python _tools/extract_skills.py --limit 1       # at most N conversations

Requires the `claude` CLI on PATH and an active login. No API key needed.
"""
from __future__ import annotations
import sys, os, re, json, argparse, subprocess
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
CONV_DIR = VAULT / "40 Resources" / "Claude Conversations"
PROPOSED_DIR = VAULT / "Skills" / "_proposed"
STATE_FILE = Path(__file__).resolve().parent / ".extract_skills_state.json"
DEFAULT_MODEL = "claude-sonnet-4-6"

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_CHARS = 350_000

PROMPT = """You are mining a saved Claude Code session to grow a self-improving SKILL memory
(reusable procedures an agent consults to ACT — distinct from facts it recalls to answer).

Extract only durable, REUSABLE PROCEDURES: repeatable techniques, operational recipes, and
gotchas-paired-with-their-fix that would help in a DIFFERENT future task. Each must be
phrased as actionable guidance: a clear trigger ("when X") and the move to make ("do Y"),
with the reason ("because Z" / the failure it prevents).

GOOD examples of skills:
- "When writing a Windows .bat that calls pm2/npm, prefix with `call` or the script exits
  after the first command."
- "Before adding a stranger to a Telegram group, send an invite link instead of force-add:
  the click is consent and it dodges PEER_FLOOD."
- "When a long-running Node service uses GramJS, schedule a daily restart; entity resolution
  degrades on multi-day sessions."

STRICTLY EXCLUDE: one-off facts or findings (those are atomic notes, not skills), specific
values/paths/ids, narrative of what happened, pleasantries, and anything not reusable next
time. Be SPARING: aim for 0-4 high-quality skills. If nothing is reusably procedural, return [].

Return ONLY a JSON array (no prose, no code fence) of objects with these fields:
  "name":        a short imperative title (e.g. "Use call when invoking pm2 from a .bat")
  "when_to_use": one line — the trigger/situation to reach for this
  "steps":       the procedure, as a single string (use "; " or newlines to separate steps)
  "why":         one line — the rationale or the failure it prevents
  "tags":        1-3 lowercase topic tags, no '#'

The conversation transcript follows after the line "===CONVERSATION===".
"""


def sanitize(name: str, maxlen: int = 90) -> str:
    name = INVALID.sub(" ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:maxlen].rstrip() or "Untitled")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def call_claude(transcript: str, model: str) -> str:
    full = PROMPT + "\n===CONVERSATION===\n" + transcript
    # On Windows, suppress the console window the `claude` CLI would otherwise spawn when this
    # runs under a windowless parent (pythonw at logon). Without this the sync pipeline pops up
    # stray, hard-to-close terminal windows. CREATE_NO_WINDOW exists only on Windows.
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run(
        ["claude", "-p", "--model", model],
        input=full, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1"},  # don't trigger vault hooks
        creationflags=creationflags,
    )
    if proc.returncode != 0:
        # See extract_notes.call_claude — `claude -p` reports API / model / usage failures on
        # STDOUT with an empty stderr, so stderr-only reporting invented "input too large" for
        # every one of the 70 conversations that failed in the 2026-07-28 run.
        err = proc.stderr.strip() or proc.stdout.strip() or "(no output on either stream)"
        raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {err[:500]}")
    return proc.stdout.strip()


def chunk_transcript(transcript: str, max_chars: int = MAX_CHARS) -> list[str]:
    if len(transcript) <= max_chars:
        return [transcript]
    turns = transcript.split("\n---\n")
    chunks, cur = [], ""
    for turn in turns:
        if cur and len(cur) + len(turn) > max_chars:
            chunks.append(cur)
            cur = turn
        else:
            cur = f"{cur}\n---\n{turn}" if cur else turn
    if cur:
        chunks.append(cur)
    return chunks


def parse_skills(raw: str) -> list[dict]:
    if not raw:
        return []
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start, end = raw.find("["), raw.rfind("]")
        candidate = raw[start:end + 1] if start != -1 and end > start else None
    if candidate is None:
        return []
    try:
        data = json.loads(candidate)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def extract_skills_from(transcript: str, model: str) -> list[dict]:
    seen, out = set(), []
    chunks = chunk_transcript(transcript)
    for i, ch in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  · chunk {i + 1}/{len(chunks)} ({len(ch):,} chars)")
        raw = call_claude(ch, model)
        for s in parse_skills(raw):
            key = (s.get("name") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(s)
    return out


def conversation_link(src: Path) -> str:
    return src.stem


# ---- write-time de-duplication -------------------------------------------------------------
# Until 2026-07-31 the only guard was `if dest.exists()` — an exact FILENAME collision. So one
# lesson phrased three ways produced three files, and a lesson re-derived from a dozen different
# conversations produced a dozen. The 654-proposal backlog contained 24 separate notes about vault
# redaction and three about `process.exitCode`, all three of those from a SINGLE transcript, split
# only because chunking made each chunk propose it under a different title.
_STOP = set("""a an the of to in on for and or is are be not with from as at by it its this that than
into you your our we my me but if then so can could should would will no use using when where which
each after before instead them they their there here what how why avoid""".split())
_INDEX: list[tuple[str, set]] | None = None


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 3 and w not in _STOP}


def _proposal_index() -> list[tuple[str, set]]:
    """(title, wordset) for every proposal already on disk. Built once per run."""
    global _INDEX
    if _INDEX is None:
        _INDEX = []
        for p in sorted(PROPOSED_DIR.glob("*.md")):
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            body = re.split(r"\n## Source\b", t)[0]
            _INDEX.append((p.stem, _words(p.stem + " " + body)))
    return _INDEX


def near_duplicate_of(skill: dict, threshold: float) -> str | None:
    """Title of an existing proposal saying the same thing, or None."""
    cand = _words(f"{skill.get('name', '')} {skill.get('steps', '')} {skill.get('why', '')}")
    if not cand:
        return None
    for title, w in _proposal_index():
        if w and len(cand & w) / len(cand | w) >= threshold:
            return title
    return None


def remember_proposal(skill: dict, title: str):
    """Add a just-written proposal to the index so the NEXT chunk can't re-propose it."""
    _proposal_index().append(
        (title, _words(f"{skill.get('name', '')} {skill.get('steps', '')} {skill.get('why', '')}")))


def write_proposed_skill(skill: dict, src: Path, date: str, dry: bool,
                         dup_threshold: float = 0.45) -> str | None:
    name = (skill.get("name") or "").strip()
    steps = (skill.get("steps") or "").strip()
    if not name or not steps:
        return None
    when = (skill.get("when_to_use") or "").strip()
    why = (skill.get("why") or "").strip()
    tags = skill.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    fname = sanitize(name) + ".md"
    dest = PROPOSED_DIR / fname
    if dest.exists():
        return None  # don't clobber an existing proposal
    dup = near_duplicate_of(skill, dup_threshold)
    if dup:
        print(f"  ~ skip (says the same as an existing proposal): {name[:56]}")
        print(f"      existing: {dup[:70]}")
        return None
    tag_lines = "\n".join(f"  - {t}" for t in (["skill/proposed"] + [str(t) for t in tags]))
    steps_md = "\n".join(f"- {part.strip()}" for part in re.split(r"\n|;\s*", steps) if part.strip())
    content = (
        "---\n"
        "type: skill\n"
        "status: proposed\n"
        f"created: {date}\n"
        f"source: \"[[{conversation_link(src)}]]\"\n"
        f"trigger: \"{when}\"\n"
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"**When to use:** {when}\n\n"
        f"## Steps\n{steps_md}\n\n"
        f"## Why\n{why}\n\n"
        "> Proposed by extract_skills.py — review, edit, then PROMOTE by moving this file up\n"
        "> into Skills/ and setting status: active. Delete if not worth keeping.\n\n"
        f"## Source\n- From conversation [[{conversation_link(src)}]]\n"
    )
    if dry:
        print(f"  + would propose: Skills/_proposed/{fname}")
        remember_proposal(skill, dest.stem)   # so a later chunk in this run can't re-propose it
        return fname
    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    remember_proposal(skill, dest.stem)
    print(f"  + Skills/_proposed/{fname}")
    return fname


def main():
    ap = argparse.ArgumentParser(description="Propose reusable skills from Claude conversations.")
    ap.add_argument("--force", action="store_true", help="reprocess conversations even if unchanged")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"claude model (default {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="don't write files, just report")
    ap.add_argument("--limit", type=int, default=0, help="process at most N conversations (0 = all)")
    ap.add_argument("--dup-threshold", type=float, default=0.45,
                    help="skip a candidate this similar to an existing proposal (0-1, default 0.45; "
                         "deliberately conservative — dropping a genuinely new skill is worse than "
                         "one extra near-duplicate)")
    args = ap.parse_args()

    sources = sorted(CONV_DIR.rglob("*.md"))
    sources = [s for s in sources if not s.name.startswith("_")]
    if not sources:
        print(f"No conversation notes in {CONV_DIR}. Run import_claude.py first.")
        return

    state = load_state()
    processed = 0
    total = 0
    for src in sources:
        key = str(src.relative_to(VAULT))
        sig = f"{src.stat().st_mtime_ns}:{src.stat().st_size}"
        if not args.force and state.get(key, {}).get("sig") == sig:
            continue
        if args.limit and processed >= args.limit:
            break
        print(f"• {src.name}")
        transcript = src.read_text(encoding="utf-8")
        transcript = re.sub(r"^---\n.*?\n---\n", "", transcript, count=1, flags=re.DOTALL)
        m = re.search(r"\d{4}-\d{2}-\d{2}", src.name)
        date = m.group(0) if m else datetime.now().strftime("%Y-%m-%d")
        try:
            skills = extract_skills_from(transcript, args.model)
        except Exception as e:
            print(f"  ! skipped ({e})")
            continue
        written = [w for s in skills
                   if (w := write_proposed_skill(s, src, date, args.dry_run, args.dup_threshold))]
        total += len(written)
        processed += 1
        if not args.dry_run:
            state[key] = {"sig": sig, "proposed": written}
            save_state(state)
        if not skills:
            print("  (no reusable procedures)")

    print(f"\nDone. Processed {processed} conversation(s), proposed {total} skill(s) into Skills/_proposed/.")
    if total and not args.dry_run:
        print("Review them and promote the keepers into Skills/ (status: active).")


if __name__ == "__main__":
    main()
