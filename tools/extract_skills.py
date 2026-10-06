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
  python tools/extract_skills.py                 # process new/changed conversations only
  python tools/extract_skills.py --force         # reprocess everything
  python tools/extract_skills.py --dry-run       # show what would be proposed
  python tools/extract_skills.py --limit 1       # at most N conversations

Requires the `claude` CLI on PATH and an active login. No API key needed.
"""
from __future__ import annotations
import sys, os, re, json, argparse, subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The checkpoint, lock, model call, chunking and field hygiene are extract_notes.py's. Two copies
# of each meant every defect in them was found twice and, as often, fixed once.
from extract_notes import (sanitize, read_state, write_state, write_item, open_state, hold_lock, content_sig,
                           is_extracted, mark_pending, source_index, captured_block, as_text, clean_tags,
                           run_claude, chunk_transcript, WORD, file_sizes, Ledger, read_transcript, unreadable)

try:
    # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
    # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
CONV_DIR = VAULT / "40 Resources" / "Claude Conversations"
PROPOSED_DIR = VAULT / "Skills" / "_proposed"
STATE_FILE = Path(__file__).resolve().parent / ".extract_skills_state.json"
# Kept in step with tools/config.py's DEFAULTS["extraction_model"], which is what the
# pipeline passes explicitly. This is only the fallback for running the tool by hand — and a
# fallback that disagrees with the configured default is a silent cost surprise.
try:
    from config import DEFAULTS as _CFG_DEFAULTS
    DEFAULT_MODEL = _CFG_DEFAULTS["extraction_model"]
except Exception:
    DEFAULT_MODEL = "claude-haiku-4-5-20251001"


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


def load_state() -> dict:
    return read_state(STATE_FILE)


def save_state(state: dict):
    write_state(STATE_FILE, state)


def call_claude(transcript: str, model: str, captured=()) -> str:
    return run_claude(PROMPT + captured_block(captured) + "\n===CONVERSATION===\n" + transcript, model)


def parse_skills(raw: str) -> list[dict]:
    """Pull a JSON array out of the model output, tolerating stray prose or a code fence.
    A genuine [] means "nothing worth keeping". Anything unparseable RAISES: returning [] made a
    truncated or malformed reply look like an empty answer, and the caller checkpointed the
    conversation as done, so it never retried (external review, 2026-09-30)."""
    if not raw:
        raise ValueError("empty model output")
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start, end = raw.find("["), raw.rfind("]")
        candidate = raw[start:end + 1] if start != -1 and end > start else None
    if candidate is None:
        raise ValueError(f"no JSON array in model output: {raw[:120]!r}")
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"model output is not valid JSON ({e.msg} at char {e.pos})") from None
    if not isinstance(data, list):
        raise ValueError(f"model output is JSON but not an array ({type(data).__name__})")
    # Every item needs its required fields, or the whole reply is treated as malformed: the writer
    # dropped an incomplete item silently and the conversation was checkpointed as done, so
    # [{"title": "An insight"}] ended as zero notes and no retry (review, 2026-09-30).
    for i, item in enumerate(data):
        missing = [k for k in ['name', 'steps'] if not (isinstance(item, dict) and isinstance(item.get(k), str) and item[k].strip())]
        if missing:
            raise ValueError(f"item {i} lacks required field(s) {', '.join(missing)}")
    return data


def extract_skills_from(transcript: str, model: str, captured=()) -> list[dict]:
    seen, out = set(), []
    chunks = chunk_transcript(transcript)
    for i, ch in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  · chunk {i + 1}/{len(chunks)} ({len(ch):,} chars)")
        raw = call_claude(ch, model, [*captured, *(s["name"] for s in out)])
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
    # Any script, not [a-z0-9]: that reduced a Russian skill to the tool names it mentions, so two
    # different procedures about pm2 and ecosystem.config.js scored 0.75 and the second was dropped.
    return {w for w in WORD.findall((s or "").lower())
            if len(w) > 3 and w not in _STOP}


# Below this many content words Jaccard is noise (3 shared words of 4 is "75% the same"), and a
# false match here silently drops a skill for good, so a short candidate is never blocked.
MIN_DUP_WORDS = 6


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
    cand = _words(f"{skill.get('name', '')} {skill.get('steps', '')} {as_text(skill.get('why'))}")
    if len(cand) < MIN_DUP_WORDS:
        return None
    for title, w in _proposal_index():
        if w and len(cand & w) / len(cand | w) >= threshold:
            return title
    return None


def remember_proposal(skill: dict, title: str):
    """Add a just-written proposal to the index so the NEXT chunk can't re-propose it."""
    _proposal_index().append(
        (title, _words(f"{skill.get('name', '')} {skill.get('steps', '')} {as_text(skill.get('why'))}")))


def split_steps(steps: str) -> list[str]:
    """One bullet per step: split on newlines and on "; ", but never inside a `code span`. Every
    ';' used to split, which cut `for f in *.log; do gzip "$f"; done` into three bullets with
    unbalanced backticks and PATH=C:\\bin;%PATH% into two."""
    out = []
    for line in steps.split("\n"):
        cur = ""
        for part in re.split(r"(`[^`\n]*`)", line):
            if len(part) > 1 and part.startswith("`") and part.endswith("`"):
                cur += part
                continue
            pieces = re.split(r";\s+", part)
            for piece in pieces[:-1]:
                out.append(cur + piece)
                cur = ""
            cur += pieces[-1]
        out.append(cur)
    return [s.strip() for s in out if s.strip()]


def write_proposed_skill(skill: dict, src: Path, date: str, dry: bool,
                         dup_threshold: float = 0.45, sig: str = "") -> str | None:
    name = as_text(skill.get("name"))
    steps = as_text(skill.get("steps"))
    if not name or not steps:
        return None
    when = re.sub(r"\s+", " ", as_text(skill.get("when_to_use")))
    why = as_text(skill.get("why"))
    tags = clean_tags(skill.get("tags") or [])
    fname = sanitize(name) + ".md"
    dest = PROPOSED_DIR / fname
    if dest.exists():
        return None  # don't clobber an existing proposal
    dup = near_duplicate_of(skill, dup_threshold)
    if dup:
        print(f"  ~ skip (says the same as an existing proposal): {name[:56]}")
        print(f"      existing: {dup[:70]}")
        return None
    tag_lines = "\n".join(f"  - {t}" for t in (["skill/proposed"] + tags))
    steps_md = "\n".join(f"- {part}" for part in split_steps(steps))
    content = (
        "---\n"
        "type: skill\n"
        "status: proposed\n"
        f"created: {date}\n"
        f"source: \"[[{conversation_link(src)}]]\"\n"
        + (f"source_hash: {sig}\n" if sig else "")
        # A JSON string is a valid YAML double-quoted scalar: a quote or a C:\\Users path in the
        # trigger, written raw, made the whole frontmatter unparseable.
        + f"trigger: {json.dumps(when, ensure_ascii=False)}\n"
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
    write_item(dest, content)
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

    if not hold_lock("extract_skills.lock"):
        print("extract_skills: another extraction run holds the lock — skipping this one")
        return
    state = open_state(load_state, STATE_FILE, args.force)
    # Promoted skills are moved up into Skills/ and keep their source: they count as taken too.
    by_source = source_index(PROPOSED_DIR, PROPOSED_DIR.parent)
    ledger = Ledger("skills")
    processed = 0
    failed: list[str] = []
    total = 0
    adopted = False
    for src in sources:
        key = str(src.relative_to(VAULT))
        prev = state.get(key, {})
        try:
            raw, stat_sig, transcript = read_transcript(src, None if args.force else prev.get("stat"))
        except (OSError, UnicodeDecodeError) as e:      # this conversation only, as in extract_notes
            print(f"• {src.name}\n  ! skipped ({unreadable(e)})")
            failed.append(src.name)
            continue
        if raw is None:
            continue
        transcript = re.sub(r"^---\n.*?\n---\n", "", transcript, count=1, flags=re.DOTALL)
        sig = content_sig(transcript)
        from_here = by_source.get(src.stem, [])
        if not args.force and is_extracted(prev, file_sizes(raw), sig,
                                           {h for _, h in from_here} | ledger.hashes(src.stem)):
            if not args.dry_run:
                state[key] = {**prev, "sig": sig, "stat": stat_sig}
                adopted = True
            continue
        if args.limit and processed >= args.limit:
            break
        print(f"• {src.name}")
        m = re.search(r"\d{4}-\d{2}-\d{2}", src.name)
        date = m.group(0) if m else datetime.now().strftime("%Y-%m-%d")
        try:
            skills = extract_skills_from(transcript, args.model, [n for n, _ in from_here])
            if skills and not args.dry_run:
                mark_pending(state, key, prev, sig, save_state)
            # Inside the try, as in extract_notes: one bad item fails this conversation only.
            written = [w for s in skills
                       if (w := write_proposed_skill(s, src, date, args.dry_run, args.dup_threshold, sig))]
        except Exception as e:
            print(f"  ! skipped ({e})")
            failed.append(src.name)      # no checkpoint (at most a pending mark), so the next run retries it
            continue
        total += len(written)
        processed += 1
        if not args.dry_run:
            by_source.setdefault(src.stem, []).extend((Path(w).stem, sig) for w in written)
            state[key] = {"sig": sig, "stat": stat_sig,
                          "proposed": list(dict.fromkeys([*prev.get("proposed", []), *written]))}
            save_state(state)
            ledger.record(src.stem, sig)
        if not skills:
            print("  (no reusable procedures)")
    if adopted:
        save_state(state)

    print(f"\nDone. Processed {processed} conversation(s), proposed {total} skill(s) into Skills/_proposed/.")
    if total and not args.dry_run:
        print("Review them and promote the keepers into Skills/ (status: active).")


    if failed:
        # Keep going past a failure, but never report the run clean. Exiting 0 here let a night
        # where EVERY conversation failed show as a clean step in sync.py (Codex review, 2026-09;
        # the 2026-07-28 run lost 70 of 136 conversations this way, unseen).
        print(f"FAILED on {len(failed)} conversation(s) — will retry next run: "
              + ", ".join(failed[:5]) + (" …" if len(failed) > 5 else ""))
        sys.exit(1)


if __name__ == "__main__":
    main()
