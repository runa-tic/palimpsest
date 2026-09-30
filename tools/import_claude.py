#!/usr/bin/env python3
"""Import Claude conversations into the Obsidian vault as one note per chat.

Two sources:
  1. Claude Code  -> local JSONL transcripts in ~/.claude/projects/
  2. claude.ai    -> conversations.json from a data export (Settings > Privacy > Export data)

Usage (run from the vault root):
  python tools/import_claude.py code                      # import all local Claude Code sessions
  python tools/import_claude.py web path/to/conversations.json
  python tools/import_claude.py code --include-thinking    # also include assistant reasoning
  python tools/import_claude.py code --include-tools       # also note tool calls

Re-running is safe: a note is only rewritten if its source changed (tracked by id).
"""
from __future__ import annotations
import sys, os, re, json, argparse, glob, hashlib, tempfile
from pathlib import Path
from datetime import datetime

# Windows consoles default to cp1252 and choke on non-Latin paths / emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT = Path(__file__).resolve().parent.parent
OUT_BASE = VAULT / "40 Resources" / "Claude Conversations"
TMP_DIR = Path(__file__).resolve().parent / "logs"   # gitignored, same filesystem as the vault

# ---------- helpers ----------

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Noise produced by the CLI that isn't real conversation:
NOISE = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.DOTALL),
    re.compile(r"<command-message>.*?</command-message>", re.DOTALL),
    re.compile(r"<command-args>.*?</command-args>", re.DOTALL),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL),
    re.compile(r"\x1b\[[0-9;]*m"),          # ANSI escape sequences
    re.compile(r"\[(?:\d{1,3})m"),          # bare ANSI codes that lost their ESC byte
]
# Turn <command-name>/foo</command-name> into a tidy "`/foo`"
CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = CMD_NAME.sub(lambda m: f"`{m.group(1).strip()}`", s)
    for pat in NOISE:
        s = pat.sub("", s)
    return s.strip()

def sanitize(name: str, maxlen: int = 80) -> str:
    # A title cut mid-emoji holds a lone surrogate, which no filesystem path can encode.
    name = (name or "").encode("utf-8", "replace").decode("utf-8")
    name = INVALID.sub(" ", name).strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:maxlen].rstrip() or "Untitled")

def redact_title(title: str) -> str:
    """The title becomes the FILENAME and the note's `# header`, so it is redacted up front and
    once — write_note() only scrubs the body, and a filename built from the raw title (the first
    60 chars of the first user message, when there is no ai-title) put deny-listed terms and
    pasted keys into the vault's file names (Codex review, 2026-09). Best-effort, like
    write_note: redaction must never break recording."""
    try:
        import redact
        return redact.redact_text(title)[0]
    except Exception:
        return title

# Transcripts stamp UTC ("...Z"); the vault's dates are local (daily notes, briefing, weekly
# review), so convert before formatting. Unconverted, a UTC+8 session at 07:30 on the 1st was
# filed under the 30th. A naive timestamp is already local and astimezone() leaves it alone.
def iso_to_date(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return ts[:10]

def iso_to_dt(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16]

def text_from_content(content, include_thinking: bool, include_tools: bool) -> str:
    """Render a message's content (string or list of blocks) to clean markdown."""
    if isinstance(content, str):
        return clean_text(content)
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            parts.append(clean_text(b.get("text", "")))
        elif bt == "thinking" and include_thinking:
            t = b.get("thinking", "").strip()
            if t:
                parts.append(f"> [!note]- 💭 Reasoning\n> " + t.replace("\n", "\n> "))
        elif bt == "tool_use" and include_tools:
            parts.append(f"`🔧 {b.get('name','tool')}`")
        # tool_result blocks (user side) are skipped as noise
    return "\n\n".join(p for p in parts if p)

def existing_note(folder: Path, sid: str) -> str | None:
    """Filename of the note already recorded for this session (matched by the "(sid).md" suffix),
    if any. The name is built from the title, and a session's title changes after it is first
    recorded (the first message, later an AI title), so building it afresh left one note per title
    for the same session; 23 sessions in the source vault had two (review, 2026-09-30). The first
    name is kept rather than renamed: atomic notes link to conversations by filename."""
    hits = sorted(folder.glob(f"* ({glob.escape(sid)}).md"), key=lambda q: q.stat().st_mtime, reverse=True) if folder.exists() else []
    return hits[0].name if hits else None

def write_note(folder: Path, fname: str, frontmatter: dict, body: str):
    folder.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            fm_lines.append(f"{k}:")
            for item in v:
                fm_lines.append(f"  - {item}")
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    out = "\n".join(fm_lines) + "\n\n" + body
    # Scrub sensitive strings (credentials + the local deny list) before the note is
    # written, so the synced log never carries them. Best-effort: redaction must never
    # break recording. See tools/redact.py.
    try:
        import redact
        out, _ = redact.redact_text(out)
    except Exception:
        pass
    dest = folder / fname
    # One newline convention, compared as bytes: read_text() turns "\r\n" into "\n", so a note
    # holding a pasted CRLF log never compared equal and was rewritten on every import. A lone
    # surrogate (an emoji cut in half in tool output) cannot be encoded; replace it rather than fail.
    data = out.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8", "replace")
    # Content-stable: don't rewrite an unchanged note, or its mtime would bust the
    # extractor's cache and trigger needless re-extraction (and duplicate notes).
    if dest.exists() and dest.read_bytes() == data:
        return
    # Atomic: write_text() truncates first, so any failure part-way left the existing
    # conversation note empty, and the next Stop hook failed the same way and kept it empty.
    # The temp file goes in the gitignored tools/logs/, not the auto-committed content folder.
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(TMP_DIR), prefix="import-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ---------- source 1: Claude Code JSONL ----------

def process_transcript(f: Path, include_thinking=False, include_tools=False, allow_sdk=False):
    """Convert one Claude Code .jsonl transcript into/updating its conversation note.
    Returns the note path if written, else None (skipped: sdk-cli run, empty, unreadable).

    allow_sdk=True keeps headless `claude -p` (sdk-cli) transcripts. The tg_bridge is a
    real conversation that runs headless, so its Stop hook passes allow_sdk=True; the
    batch importer leaves it False so internal ask/extract tool-runs stay filtered out."""
    try:
        raw = f.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Skip headless `claude -p` / SDK tool-runs (our own extract/ask calls write
    # transcripts too). Real conversations use entrypoint cli / claude-desktop.
    # The bridge is headless but a real conversation, so it sets allow_sdk.
    if not allow_sdk and any(r.get("entrypoint") == "sdk-cli" for r in rows):
        return None

    title = None
    first_ts = last_ts = None
    version = None
    turns = []
    for d in rows:
        t = d.get("type")
        if t == "ai-title" and not title:
            title = d.get("aiTitle")
        if t in ("user", "assistant"):
            if d.get("isSidechain"):
                continue  # skip subagent side threads
            msg = d.get("message", {})
            role = msg.get("role")
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            version = version or d.get("version")
            rendered = text_from_content(msg.get("content"), include_thinking, include_tools)
            if rendered:
                turns.append((role, rendered))

    if not turns:
        return None
    # Redact BEFORE truncating: a key straddling char 60 would be cut into a fragment no
    # credential pattern matches, and its head would land in the filename.
    title = redact_title(title) if title else next(
        (redact_title(txt)[:60] for role, txt in turns if role == "user"), "Untitled")

    proj_label = f.parent.name.split("-")[-1] or f.parent.name
    date = iso_to_date(first_ts)
    folder = OUT_BASE / "Claude Code" / proj_label
    fname = existing_note(folder, f.stem[:8]) or f"{date} {sanitize(title)} ({f.stem[:8]}).md"

    body_parts = [f"# {title}\n"]
    for role, txt in turns:
        who = "🧑 **Me**" if role == "user" else "🤖 **Claude**"
        body_parts.append(f"### {who}\n\n{txt}")
    body = "\n\n---\n\n".join(body_parts)

    fm = {
        "type": "claude-conversation",
        "source": "claude-code",
        "project": proj_label,
        "date": date,
        "started": iso_to_dt(first_ts),
        "ended": iso_to_dt(last_ts),
        "session_id": f.stem,
        "claude_version": version or "",
        "tags": ["claude/conversation", "claude/code", f"project/{sanitize(proj_label).replace(' ', '-')}"],
    }
    write_note(folder, fname, fm, body)
    return folder / fname

def import_code(args):
    projects = Path.home() / ".claude" / "projects"
    # Scope to THIS vault's own project by default. Globbing */*.jsonl imports every Claude
    # Code conversation on the machine, from every unrelated project — measured on a fresh
    # install: 155 conversations, 134 of them from a different vault entirely. That is a
    # privacy surprise (a work vault silently absorbing personal project transcripts), and it
    # makes the first sync cost one model call per conversation for material the user never
    # asked to distil. Claude Code names each project directory by slugifying its path.
    if getattr(args, "all_projects", False):
        files = sorted(projects.glob("*/*.jsonl"))
        scope = "ALL projects on this machine"
    else:
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(VAULT))
        files = sorted((projects / slug).glob("*.jsonl"))
        scope = f"this vault only ({slug})"
    print(f"  import scope: {scope}"
          + ("" if getattr(args, "all_projects", False)
             else " — use --all-projects to widen"))
    if not files:
        print(f"No transcripts found under {projects}"
              + ("" if getattr(args, "all_projects", False) else f"/{slug}"))
        return
    count, failed = 0, []
    for f in files:
        # One transcript that cannot be written must not abort the batch before the rest.
        try:
            count += bool(process_transcript(f, args.include_thinking, args.include_tools))
        except Exception as e:
            print(f"  ! {f.name}: {type(e).__name__}: {e}")
            failed.append(f.name)
    print(f"Imported {count} Claude Code conversation(s) into {OUT_BASE / 'Claude Code'}")
    if failed:
        print(f"FAILED on {len(failed)} transcript(s): " + ", ".join(failed[:5]) + (" …" if len(failed) > 5 else ""))
        sys.exit(1)

def import_file(args):
    """Import a single transcript by path (used by the live-recording Stop hook)."""
    r = process_transcript(Path(args.path), args.include_thinking, args.include_tools)
    print(f"recorded: {r}" if r else "skipped (sdk-cli, empty, or unreadable)")

# ---------- source 2: claude.ai export ----------

def import_web(args):
    path = Path(args.export)
    if path.is_dir():
        cand = path / "conversations.json"
        path = cand if cand.exists() else path
    if not path.exists():
        print(f"File not found: {path}")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("conversations", [data])
    count = 0
    for conv in data:
        name = redact_title(conv.get("name") or "Untitled")
        created = conv.get("created_at")
        updated = conv.get("updated_at")
        uuid = conv.get("uuid") or conv.get("id") or ""
        if not uuid:
            # The note is found again by its "(id).md" suffix, so an empty id made every
            # uuid-less conversation match the first one's note and overwrite it. Derive a
            # stable one from what identifies the conversation instead.
            first = next((str(m.get("text") or m.get("content") or "") for m in
                          (conv.get("chat_messages") or conv.get("messages") or []) if isinstance(m, dict)), "")
            uuid = hashlib.sha1(json.dumps([created, conv.get("name"), first], default=str)
                                .encode("utf-8", "replace")).hexdigest()
        uuid = str(uuid)
        msgs = conv.get("chat_messages") or conv.get("messages") or []
        turns = []
        for m in msgs:
            sender = m.get("sender") or m.get("role")
            role = "user" if sender in ("human", "user") else "assistant"
            txt = m.get("text") or ""
            if not txt and isinstance(m.get("content"), list):
                txt = "\n\n".join(
                    c.get("text", "") for c in m["content"] if isinstance(c, dict) and c.get("type") == "text"
                )
            txt = clean_text(txt)
            if txt:
                turns.append((role, txt))
        if not turns:
            continue
        date = iso_to_date(created)
        fname = existing_note(OUT_BASE / "claude.ai", uuid[:8]) or f"{date} {sanitize(name)} ({uuid[:8]}).md"
        body_parts = [f"# {name}\n"]
        for role, txt in turns:
            who = "🧑 **Me**" if role == "user" else "🤖 **Claude**"
            body_parts.append(f"### {who}\n\n{txt}")
        body = "\n\n---\n\n".join(body_parts)
        fm = {
            "type": "claude-conversation",
            "source": "claude.ai",
            "date": date,
            "started": iso_to_dt(created),
            "ended": iso_to_dt(updated),
            "conversation_id": uuid,
            "tags": ["claude/conversation", "claude/web"],
        }
        write_note(OUT_BASE / "claude.ai", fname, fm, body)
        count += 1
    print(f"Imported {count} claude.ai conversation(s) into {OUT_BASE / 'claude.ai'}")

# ---------- cli ----------

def main():
    ap = argparse.ArgumentParser(description="Import Claude conversations into Obsidian.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("code", help="import local Claude Code transcripts")
    pc.add_argument("--include-thinking", action="store_true", help="include assistant reasoning (collapsible)")
    pc.add_argument("--include-tools", action="store_true", help="note tool calls")
    pc.add_argument("--all-projects", action="store_true",
                    help="import EVERY Claude Code project on this machine, not just this "
                         "vault's. Off by default: it pulls in unrelated projects' transcripts "
                         "and costs one distillation call per conversation.")
    pw = sub.add_parser("web", help="import a claude.ai conversations.json export")
    pw.add_argument("export", help="path to conversations.json (or the unzipped export folder)")
    pw.add_argument("--include-thinking", action="store_true")
    pw.add_argument("--include-tools", action="store_true")
    pf = sub.add_parser("file", help="import a single transcript by path (used by the Stop hook)")
    pf.add_argument("path", help="path to a .jsonl transcript")
    pf.add_argument("--include-thinking", action="store_true")
    pf.add_argument("--include-tools", action="store_true")
    args = ap.parse_args()
    if args.cmd == "code":
        import_code(args)
    elif args.cmd == "web":
        import_web(args)
    else:
        import_file(args)

if __name__ == "__main__":
    main()
