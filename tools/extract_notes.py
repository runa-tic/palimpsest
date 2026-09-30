#!/usr/bin/env python3
"""Extract atomic notes from imported Claude conversations using the local `claude` CLI.

For each conversation note in "40 Resources/Claude Conversations/", asks Claude to pull out
durable, reusable insights and writes each as an atomic note in "10 Notes/", linked back to
its source conversation.

Usage (from vault root):
  python tools/extract_notes.py                 # process new/changed conversations only
  python tools/extract_notes.py --force         # reprocess everything
  python tools/extract_notes.py --model claude-haiku-4-5-20251001   # cheaper/faster
  python tools/extract_notes.py --dry-run       # show what would be written

Requires the `claude` CLI on PATH and an active login. No API key needed.
"""
from __future__ import annotations
import sys, os, re, json, argparse, subprocess, hashlib
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
CONV_DIR = VAULT / "40 Resources" / "Claude Conversations"
NOTES_DIR = VAULT / "10 Notes"
STATE_FILE = Path(__file__).resolve().parent / ".extract_state.json"
DEFAULT_MODEL = "claude-sonnet-4-6"

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

PROMPT = """You are mining a saved Claude conversation for a personal knowledge base (a "second brain").

Extract the DURABLE, REUSABLE insights: general principles, techniques, gotchas, mental
models, and decisions-with-rationale that would be useful again in a DIFFERENT project.

ALSO KEEP concrete empirical results and verified findings from ongoing research or projects,
even when they are specific to one project: measured outcomes, what an experiment confirmed or
ruled out, a signal's strength and how it behaves (e.g. "feature X explains ~38% of the
contemporaneous move but its forward predictive power decays to ~0 within seconds"). Capture
the finding together with the transferable principle it implies.

STRICTLY EXCLUDE: file paths, environment/setup trivia, one-off bug fixes, pleasantries,
restated questions, and passing details that only matter inside this one conversation.

Write each as an ATOMIC note: one self-contained idea, in plain language, understandable
without opening the conversation. Aim for 0-6 notes. If there is nothing durable, return [].

Return ONLY a JSON array (no prose, no code fence) of objects with these fields:
  "title":  a concise claim stated as the insight itself (e.g. "Prompt caching cuts repeated-context cost")
  "body":   2-5 sentences elaborating the idea in your own words
  "tags":   1-3 lowercase topic tags, no '#', e.g. ["llm","prompting"]
  "volatility": exactly one of "timeless", "dated", or "live" — how this note decays:
      "timeless" — a principle, mechanism or trade-off that stays true regardless of when it
          is read. "A deny list only protects the writer that runs it."
      "dated" — true AS OF the conversation, and quietly wrong later: current deployment or
          commit state, prices, model ids and their costs, library versions, API shapes,
          measured numbers from a system that keeps changing, "X is broken / not yet done".
      "live" — the durable part is a pointer that must be re-read to be trusted: a dashboard,
          a queue, a leaderboard, an upstream doc.
    When torn between "timeless" and "dated", choose "dated". A stale note believed to be
    timeless is the expensive failure; a timeless note flagged for review costs a glance.

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

MAX_CHARS = 350_000  # keep a single request comfortably within the context window

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
        # `claude -p` puts API / model / usage errors on STDOUT with rc=1 and an EMPTY stderr;
        # only argv parsing errors go to stderr (verified 2026-07-31: an invalid --model gives
        # 162 chars on stdout, 0 on stderr; an invalid flag gives 0/41). Reading stderr alone
        # and guessing "input too large" discarded the real reason for 70 failed conversations
        # and sent two separate investigations chasing chunk sizes. Report both streams.
        err = proc.stderr.strip() or proc.stdout.strip() or "(no output on either stream)"
        raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {err[:500]}")
    return proc.stdout.strip()

def chunk_transcript(transcript: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split a long transcript on turn boundaries so each chunk fits in one request."""
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

def extract_notes_from(transcript: str, model: str) -> list[dict]:
    """Run extraction over one or more chunks and dedupe notes by title."""
    seen, out = set(), []
    chunks = chunk_transcript(transcript)
    for i, ch in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  · chunk {i + 1}/{len(chunks)} ({len(ch):,} chars)")
        raw = call_claude(ch, model)
        for n in parse_notes(raw):
            key = (n.get("title") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(n)
    return out

def parse_notes(raw: str) -> list[dict]:
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
    return data

def conversation_link(src: Path) -> str:
    # Obsidian resolves links by basename, so the stem alone is enough.
    return src.stem


# ---- near-duplicate ANNOTATION (deliberately not a block) -----------------------------------
# extract_skills.py refuses to write a candidate too similar to an existing proposal. The same
# gate is NOT safe for atomic notes, and that is a measured conclusion rather than a hunch.
# Calibrated 2026-07-31 over 1,016 notes, comparing pairs inside dedupe.py's duplicate families
# against 4,000 random pairs, using dedupe's own weighted score:
#
#     threshold   duplicates caught   expected FALSE blocks per new note
#       0.35            31%                    0.51
#       0.40            14%                    0.25
#       0.45             6%                    0.00
#
# A blocking gate can therefore catch at most ~6% of real duplicates without silently
# discarding genuine new knowledge — every candidate is compared against all 1,016 notes, so a
# tiny per-pair error rate becomes a large per-note one. Notes are long-form and share
# vocabulary; skills are short and formulaic, which is why the same instrument works there and
# fails here. So: never refuse, just record the resemblance in the note's own frontmatter, where
# dedupe.py's family report and a human reviewer will both find it.
DUP_NOTE_THRESHOLD = 0.30
_NOTE_INDEX = None


def _nwords(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 3 and w not in _NSTOP}


_NSTOP = set("""a an the of to in on for and or is are be not with from as at by it its this that than
into you your our we my me but if then so can could should would will no use using when where which
each after before instead them they their there here what how why avoid""".split())


def _note_index():
    global _NOTE_INDEX
    if _NOTE_INDEX is None:
        _NOTE_INDEX = []
        for p in sorted(NOTES_DIR.glob("*.md")):
            if p.name.startswith("_"):
                continue
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fm = re.match(r"---\n(.*?)\n---", t, re.DOTALL)
            tags = set(re.findall(r"^\s+- (.+?)\s*$", fm.group(1), re.M)) if fm else set()
            tags.discard("claude/extracted")
            body = re.split(r"\n## Source\b", t[fm.end():] if fm else t)[0]
            _NOTE_INDEX.append((p.stem, _nwords(p.stem), _nwords(body), tags))
    return _NOTE_INDEX


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def similar_existing_note(title: str, body: str, tags) -> tuple[str, float] | None:
    """Closest existing note, by dedupe.py's weighting. Advisory only — never blocks a write."""
    tw, bw, tg = _nwords(title), _nwords(body), set(tags or [])
    best, bn = 0.0, None
    for name, otw, obw, otg in _note_index():
        s = 0.5 * _jac(bw, obw) + 0.35 * _jac(tw, otw) + 0.15 * _jac(tg, otg)
        if s > best:
            best, bn = s, name
    return (bn, best) if bn and best >= DUP_NOTE_THRESHOLD else None


def remember_note(title: str, body: str, tags):
    # Index under the SANITISED stem, which is what the file is actually named. Storing the raw
    # title meant a later note could match it and emit `similar_to: "[[...cost/latency...]]"` —
    # an unfollowable link, because sanitize() had stripped the slash from the filename. Found
    # 2026-07-31 as a dangling reference in the vault-health broken-link list.
    stem = sanitize(title)
    _note_index().append((stem, _nwords(stem), _nwords(body), set(tags or [])))

def write_atomic_note(note: dict, src: Path, date: str, dry: bool) -> str | None:
    title = (note.get("title") or "").strip()
    body = (note.get("body") or "").strip()
    if not title or not body:
        return None
    tags = note.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    fname = sanitize(title) + ".md"
    dest = NOTES_DIR / fname
    if dest.exists():
        return None  # keep existing note; don't clobber
    near = similar_existing_note(title, body, tags)
    sim_line = ""
    if near:
        sim_line = f"similar_to: \"[[{near[0]}]]\"\nsimilarity: {near[1]:.2f}\n"
        print(f"  ! near-duplicate of an existing note ({near[1]:.0%}): {near[0][:60]}")
    tag_lines = "\n".join(f"  - {t}" for t in (["claude/extracted"] + [str(t) for t in tags]))
    # How this claim decays. Notes carrying operational state read exactly like notes carrying
    # principles, so a vault silently accumulates confident statements that stopped being true
    # months ago — in the vault this came from, notes asserting "UNCOMMITTED" were wrong by the time anyone
    # relied on them. Recording shelf life at write time is the only cheap moment to do it;
    # nobody classifies 954 notes later. "unknown" when the model declines to choose, so the
    # gap stays visible instead of defaulting into a lie.
    vol = str(note.get("volatility") or "").strip().lower()
    if vol not in ("timeless", "dated", "live"):
        vol = "unknown"
    content = (
        "---\n"
        "type: note\n"
        f"created: {date}\n"
        f"volatility: {vol}\n"
        f"source: \"[[{conversation_link(src)}]]\"\n"
        f"{sim_line}"
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n\n"
        + (f"> [!info]- Possible duplicate\n"
           f"> Written {date} despite closely resembling [[{near[0]}]] ({near[1]:.0%} similar).\n"
           f"> Extraction never refuses a note — see DUP_NOTE_THRESHOLD in `tools/extract_notes.py`\n"
           f"> for why blocking is unsafe here. Merge or delete one of the two if they say the "
           f"same thing.\n\n" if near else "")
        + "## Related\n- [[ ]]\n\n"
        f"## Source\n- From conversation [[{conversation_link(src)}]]\n"
    )
    if dry:
        print(f"  + would write: 10 Notes/{fname}")
        remember_note(title, body, tags)
        return fname
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    remember_note(title, body, tags)   # so a later note in this same run sees it too
    print(f"  + 10 Notes/{fname}")
    return fname

def main():
    ap = argparse.ArgumentParser(description="Extract atomic notes from Claude conversations.")
    ap.add_argument("--force", action="store_true", help="reprocess conversations even if unchanged")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"claude model (default {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="don't write files, just report")
    ap.add_argument("--limit", type=int, default=0, help="process at most N conversations (0 = all)")
    args = ap.parse_args()

    sources = sorted(CONV_DIR.rglob("*.md"))
    sources = [s for s in sources if not s.name.startswith("_")]
    if not sources:
        print(f"No conversation notes in {CONV_DIR}. Run import_claude.py first.")
        return

    state = load_state()
    processed = 0
    failed: list[str] = []
    total_notes = 0
    for src in sources:
        key = str(src.relative_to(VAULT))
        sig = f"{src.stat().st_mtime_ns}:{src.stat().st_size}"
        if not args.force and state.get(key, {}).get("sig") == sig:
            continue
        if args.limit and processed >= args.limit:
            break
        print(f"• {src.name}")
        transcript = src.read_text(encoding="utf-8")
        # strip the frontmatter of the conversation note before sending
        transcript = re.sub(r"^---\n.*?\n---\n", "", transcript, count=1, flags=re.DOTALL)
        date = re.search(r"\d{4}-\d{2}-\d{2}", src.name)
        date = date.group(0) if date else datetime.now().strftime("%Y-%m-%d")
        try:
            notes = extract_notes_from(transcript, args.model)
        except Exception as e:
            print(f"  ! skipped ({e})")
            failed.append(src.name)      # not recorded in state, so the next run retries it
            continue
        written = [w for n in notes if (w := write_atomic_note(n, src, date, args.dry_run))]
        total_notes += len(written)
        processed += 1
        if not args.dry_run:
            state[key] = {"sig": sig, "notes": written}
            save_state(state)
        if not notes:
            print("  (no durable insights)")

    print(f"\nDone. Processed {processed} conversation(s), wrote {total_notes} atomic note(s).")

    if failed:
        # Keep going past a failure, but never report the run clean. Exiting 0 here let a night
        # where EVERY conversation failed show as a clean step in sync.py (Codex review, 2026-09;
        # the 2026-07-28 run lost 70 of 136 conversations this way, unseen).
        print(f"FAILED on {len(failed)} conversation(s) — will retry next run: "
              + ", ".join(failed[:5]) + (" …" if len(failed) > 5 else ""))
        sys.exit(1)


if __name__ == "__main__":
    main()
