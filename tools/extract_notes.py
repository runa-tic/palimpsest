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
import sys, os, re, json, argparse, subprocess, hashlib, shutil, tempfile, socket, stat
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
# Locks and temp files live in tools/logs/, which is gitignored: per machine, never committed.
LOCK_DIR = Path(__file__).resolve().parent / "logs"
# What each machine has extracted, by conversation and content hash: committed (State/ is vault
# content), one file per machine and kind so the two machines never edit the same file.
EXTRACTED_DIR = VAULT / "State" / "extracted"
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

def no_surrogates(s: str) -> str:
    """A lone surrogate (a model's "\\ud83d", an emoji cut in half) cannot be encoded to UTF-8:
    it made the file name, then the note's text, raise UnicodeEncodeError on every run."""
    return s.encode("utf-8", "replace").decode("utf-8")


def sanitize(name: str, maxlen: int = 90, maxbytes: int = 200) -> str:
    name = INVALID.sub(" ", no_surrogates(name or "")).strip()
    # A leading "." makes a dotfile Obsidian hides, and a leading "_" is how every reader here
    # (ask.gather, _note_index, dedupe) marks a file to skip: "__slots__ ..." was written but
    # never retrieved or deduped.
    name = re.sub(r"\s+", " ", name).lstrip("._ ")
    name = name[:maxlen].rstrip()
    # The cap is characters, but NAME_MAX is 255 BYTES on ext4: 90 CJK characters are 270 bytes,
    # and the ENAMETOOLONG killed the whole run. Trim to a byte budget that leaves room for ".md".
    while len(name.encode("utf-8")) > maxbytes:
        name = name[:-1].rstrip()
    return name or "Untitled"


class StateCorrupt(ValueError):
    pass


def read_state(path: Path) -> dict:
    """The checkpoint, or {} when there is none yet. An unreadable one RAISES: treating a file
    truncated by a kill mid-write as {} re-sent every conversation to the model and wrote a fresh
    near-duplicate of every note, and the run still exited 0."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise StateCorrupt(f"{path.name} is unreadable ({e})") from None
    if not isinstance(data, dict):
        raise StateCorrupt(f"{path.name} is not a JSON object")
    return data


def default_mode(path: Path) -> int:
    """The mode a plain write would give `path`: its current one, or 0o666 less the umask.
    mkstemp creates 0600, which os.replace would carry onto a shared, committed file."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mask = os.umask(0)
        os.umask(mask)
        return 0o666 & ~mask


def write_state(path: Path, state: dict, mode: int | None = None):
    # Atomic: write a temp file, then rename over. Rewriting in place left a truncated file
    # whenever sync.py's timeout killed the step mid-write. The temp file sits in the gitignored
    # logs dir (same filesystem), so one orphaned by a kill is never committed.
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LOCK_DIR), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state() -> dict:
    return read_state(STATE_FILE)

def save_state(state: dict):
    write_state(STATE_FILE, state)


def open_state(load, path: Path, force: bool) -> dict:
    """load() or a loud stop. With --force (which reprocesses everything anyway) the bad file is
    moved aside, not deleted, and the run starts over."""
    try:
        return load()
    except StateCorrupt as e:
        if not force:
            print(f"ERROR: {e}. Refusing to start from an empty checkpoint: that re-extracts every "
                  f"conversation and duplicates their notes. Repair or remove {path}, or rerun with --force.")
            sys.exit(1)
        aside = path.with_name(path.name + ".corrupt")
        os.replace(path, aside)
        print(f"! {e}; moved it to {aside.name} and starting over (--force)")
        return {}


_LOCKS = []   # held open for the life of the process; the OS releases them if it is killed


def hold_lock(name: str) -> bool:
    """One extraction run of each kind at a time. Two overlapping runs (a manual one and the
    scheduled sync) each sent every pending conversation to the model, wrote both wordings, and
    the last save dropped the other run's checkpoints. An OS lock, not an O_EXCL file, so a run
    that sync.py kills on timeout cannot leave a stale lock behind."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_DIR / name, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCKS.append(fh)
    return True


def content_sig(transcript: str) -> str:
    """Checkpoint key: the text sent to the model, not the file's mtime. Git does not keep mtimes,
    so a push-time rebase or a pull re-extracted conversations that had not changed; line endings
    are normalised so a CRLF checkout hashes the same as the machine that wrote the note."""
    norm = transcript.replace("\r\n", "\n").replace("\r", "\n")
    return "sha1:" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def file_sizes(raw: bytes) -> set[int]:
    """The sizes this file has had with the same text: as it is, all-LF, and all-CRLF. The box
    wrote conversation notes with CRLF (write_text on Windows); a checkout that renormalises line
    endings changes the size of a note whose text did not change."""
    lf = raw.replace(b"\r\n", b"\n")
    return {len(raw), len(lf), len(lf) + lf.count(b"\n")}


def is_extracted(prev: dict, sizes: set, sig: str, done_elsewhere: set) -> bool:
    """Whether this content was already extracted, here or on another machine."""
    if prev.get("sig") == sig or sig in done_elsewhere:
        return True
    # An entry from before content hashing holds "mtime_ns:size". The same size means only the
    # mtime moved (a rebase, or a frontmatter-only re-import): adopt it instead of re-sending every
    # conversation in the vault once on upgrade. Growth changes the size; a note whose only
    # change is CRLF <-> LF counts as the same size.
    old = str(prev.get("sig") or "")
    return bool(re.fullmatch(r"\d+:\d+", old)) and int(old.split(":")[1]) in sizes


def machine_name() -> str:
    """This machine's name, as state.py names it: palimpsest.json "machine", else
    $PALIMPSEST_MACHINE, else the short hostname."""
    name = None
    try:
        import config as _cfg
        name = _cfg.load().get("machine")
    except Exception:
        pass
    name = name or os.environ.get("PALIMPSEST_MACHINE") or socket.gethostname().split(".")[0]
    return re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-") or "machine"


class Ledger:
    """{conversation stem: content hash} of what each machine has extracted, committed as
    State/extracted/<kind>-<machine>.json. The notes' source_hash covers a pass that wrote
    something; a pass that returned nothing (the usual outcome once a grown conversation is sent
    with its ALREADY CAPTURED list) left no trace in git, so the other machine paid one model call
    for every growth increment this one had already handled. One writer per file: no merge."""

    def __init__(self, kind: str):
        self.kind = kind
        self.path = EXTRACTED_DIR / f"{kind}-{machine_name()}.json"
        self.mine: dict = {}
        self.all: dict = {}
        for p in sorted(EXTRACTED_DIR.glob(f"{kind}-*.json")) if EXTRACTED_DIR.exists() else []:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                # Only an optimisation: a bad file costs model calls, never correctness.
                print(f"  ! ignoring {p.name} ({e})")
                continue
            if not isinstance(data, dict):
                continue
            if p == self.path:
                self.mine = dict(data)
            for stem, h in data.items():
                self.all.setdefault(stem, set()).add(str(h))

    def hashes(self, stem: str) -> set:
        return self.all.get(stem, set())

    def record(self, stem: str, sig: str):
        self.mine[stem] = sig
        self.all.setdefault(stem, set()).add(sig)
        EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
        write_state(self.path, dict(sorted(self.mine.items())), default_mode(self.path))


def source_index(*dirs: Path) -> dict:
    """{conversation stem: [(note stem, source_hash or "")]} from the notes' own frontmatter. These
    travel with git, so a machine that never ran this conversation still sees what was taken from
    it — the local checkpoint is per machine."""
    out: dict = {}
    for d in dirs:
        for p in sorted(d.glob("*.md")) if d.exists() else []:
            try:
                head = p.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                continue
            fm = re.match(r"---\n(.*?)\n---", head, re.DOTALL)
            src = re.search(r'^source:\s*"?\[\[(.+?)\]\]', fm.group(1), re.M) if fm else None
            if src:
                h = re.search(r"^source_hash:\s*(\S+)", fm.group(1), re.M)
                out.setdefault(src.group(1), []).append((p.stem, h.group(1) if h else ""))
    return out


def captured_block(titles) -> str:
    """Tell the model what this conversation already yielded. A conversation that grows is sent
    again in full, and without this the model restated its day-one insights under new titles,
    which only an exact filename match could have caught."""
    titles = [t for t in dict.fromkeys(titles) if t]
    if not titles:
        return ""
    return ("\nALREADY CAPTURED from this conversation on an earlier pass. Do NOT return these again, "
            "even reworded; return only what they do not cover:\n"
            + "".join(f"- {t}\n" for t in titles))


def as_text(v) -> str:
    """A model field as one line of text; a list or number where a string was asked for is
    common, and .strip() on it crashed the whole run. Lone surrogates are replaced."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(as_text(x) for x in v).strip()
    return no_surrogates(str(v)).strip()


def clean_tags(tags) -> list[str]:
    """Tags as safe YAML list items. Written raw, '*nix' or '@types' made the frontmatter
    unparseable, and a nested list crashed set(tags)."""
    out = []
    for t in tags if isinstance(tags, list) else [tags]:
        if isinstance(t, bool) or not isinstance(t, (str, int, float)):
            continue
        t = re.sub(r"[^\w/-]+", "-", str(t).strip().lower()).strip("-/")
        if t and t not in out:
            out.append(t)
    return out


# Extraction needs no tools, and the transcript it reads is untrusted text (a pasted email, a web
# page). Run in the vault, `claude -p` loaded the vault's CLAUDE.md and project allowlist, whose
# protocol is "Grep/Read the vault, run the tools" — so mined text could steer it into reading
# gitignored local files into a note, or running an allowlisted tool. The boundary is an empty
# allowlist, not a denylist: `--tools ""` turns off every built-in tool, --strict-mcp-config loads
# no MCP server from any settings file, and ENABLE_CLAUDEAI_MCP_SERVERS=false keeps claude.ai
# connectors out. --no-session-persistence writes no transcript: hundreds of calls a night used to
# land in ~/.claude/projects/ (and, run from the vault, in the folder import_claude reads).
DENIED_TOOLS = ["Bash", "Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit",
                "WebFetch", "WebSearch", "Task", "Agent", "Skill", "TodoWrite"]
STRICT_FLAGS = ["--strict-mcp-config", "--no-session-persistence"]
_LEGACY_CLI = False   # set once an older CLI rejects the strict flags


def claude_argv(exe: str, model: str, legacy: bool = False) -> list[str]:
    # --tools takes a variadic list, so it goes last with its one (empty) value.
    base = [exe, "-p", "--model", model, "--disallowedTools", ",".join(DENIED_TOOLS)]
    return base if legacy else base[:4] + STRICT_FLAGS + base[4:] + ["--tools", ""]


def extract_cwd() -> Path:
    """One fixed, empty, per-user directory outside the vault to run `claude` in, so it finds no
    project CLAUDE.md or settings. Fixed rather than a fresh mkdtemp per call: each temp path was a
    new project to Claude Code, and a run killed mid-call (sync.py's timeout) leaked its dir. The
    temp dir is per user on macOS and Windows; on a shared /tmp the name carries the uid and the
    directory must be this user's own and private, or another user could plant a CLAUDE.md in it."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    d = Path(tempfile.gettempdir()) / ("palimpsest-extract" + (f"-{uid}" if uid is not None else ""))
    d.mkdir(mode=0o700, exist_ok=True)
    if uid is not None:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o022:
            raise RuntimeError(f"{d} is not a private directory owned by this user; remove it")
    vault, dr = VAULT.resolve(), d.resolve()
    if vault == dr or vault in dr.parents:
        raise RuntimeError(f"extraction cwd {d} is inside the vault; point TMPDIR elsewhere")
    return d


def run_claude(prompt: str, model: str) -> str:
    global _LEGACY_CLI
    # shutil.which honours PATHEXT: a bare "claude" argv does not find npm's claude.cmd on Windows.
    exe = shutil.which("claude") or "claude"
    # On Windows, suppress the console window the `claude` CLI would otherwise spawn when this
    # runs under a windowless parent (pythonw at logon). Without this the sync pipeline pops up
    # stray, hard-to-close terminal windows. CREATE_NO_WINDOW exists only on Windows.
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    env = {**os.environ, "CLAUDE_BRAIN_NO_HOOK": "1",   # don't trigger vault hooks
           "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    cwd = extract_cwd()
    for legacy in ([True] if _LEGACY_CLI else [False, True]):
        proc = subprocess.run(
            claude_argv(exe, model, legacy),
            input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=str(cwd), env=env,
            creationflags=creationflags,
        )
        if legacy or proc.returncode == 0 or "unknown option" not in proc.stderr:
            break
        # A CLI older than --tools / --no-session-persistence: fall back to the denylist, and say so.
        _LEGACY_CLI = True
        print(f"  ! this claude CLI rejects the strict flags ({proc.stderr.strip()[:120]}); "
              f"falling back to --disallowedTools only — update the CLI")
    if proc.returncode != 0:
        # `claude -p` puts API / model / usage errors on STDOUT with rc=1 and an EMPTY stderr;
        # only argv parsing errors go to stderr (verified 2026-07-31: an invalid --model gives
        # 162 chars on stdout, 0 on stderr; an invalid flag gives 0/41). Reading stderr alone
        # and guessing "input too large" discarded the real reason for 70 failed conversations
        # and sent two separate investigations chasing chunk sizes. Report both streams.
        err = proc.stderr.strip() or proc.stdout.strip() or "(no output on either stream)"
        raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {err[:500]}")
    return proc.stdout.strip()

MAX_CHARS = 350_000  # keep a single request comfortably within the context window

def call_claude(transcript: str, model: str, captured=()) -> str:
    return run_claude(PROMPT + captured_block(captured) + "\n===CONVERSATION===\n" + transcript, model)

def _split_long(turn: str, max_chars: int) -> list[str]:
    """A single turn longer than the limit (a pasted log, a generated file), cut on paragraph,
    then line boundaries, then hard. Left whole it failed `claude -p` on every run, forever."""
    if len(turn) <= max_chars:
        return [turn]
    for sep in ("\n\n", "\n"):
        parts = turn.split(sep)
        if len(parts) > 1:
            out, cur = [], ""
            for p in parts:
                if cur and len(cur) + len(sep) + len(p) > max_chars:
                    out.append(cur)
                    cur = p
                else:
                    cur = f"{cur}{sep}{p}" if cur else p
            out.append(cur)
            return [q for piece in out for q in _split_long(piece, max_chars)]
    return [turn[i:i + max_chars] for i in range(0, len(turn), max_chars)]

def chunk_transcript(transcript: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split a long transcript on turn boundaries so each chunk fits in one request."""
    if len(transcript) <= max_chars:
        return [transcript]
    sep = "\n---\n"
    turns = [piece for turn in transcript.split(sep) for piece in _split_long(turn, max_chars)]
    chunks, cur = [], ""
    for turn in turns:
        if cur and len(cur) + len(sep) + len(turn) > max_chars:   # the separator counts too
            chunks.append(cur)
            cur = turn
        else:
            cur = f"{cur}{sep}{turn}" if cur else turn
    if cur:
        chunks.append(cur)
    return chunks

def extract_notes_from(transcript: str, model: str, captured=()) -> list[dict]:
    """Run extraction over one or more chunks and dedupe notes by title. `captured` are titles
    this conversation already yielded; each chunk also sees what the earlier chunks returned."""
    seen, out = set(), []
    chunks = chunk_transcript(transcript)
    for i, ch in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  · chunk {i + 1}/{len(chunks)} ({len(ch):,} chars)")
        raw = call_claude(ch, model, [*captured, *(n["title"] for n in out)])
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
    # Every item needs its required fields, or the whole reply is treated as malformed: the writer
    # dropped an incomplete item silently and the conversation was checkpointed as done, so
    # [{"title": "An insight"}] ended as zero notes and no retry (review, 2026-09-30).
    for i, item in enumerate(data):
        missing = [k for k in ['title', 'body'] if not (isinstance(item, dict) and isinstance(item.get(k), str) and item[k].strip())]
        if missing:
            raise ValueError(f"item {i} lacks required field(s) {', '.join(missing)}")
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


WORD = re.compile(r"[^\W_]+")   # letters and digits in any script: the vault is bilingual


def _nwords(s: str) -> set:
    # [a-z0-9]+ reduced a Russian note to its few Latin tool names, so unrelated notes that
    # mention the same tools scored as near-duplicates and a purely Cyrillic one scored as nothing.
    return {w for w in WORD.findall((s or "").lower())
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

def write_atomic_note(note: dict, src: Path, date: str, dry: bool, sig: str = "") -> str | None:
    title = as_text(note.get("title"))
    body = as_text(note.get("body"))
    if not title or not body:
        return None
    tags = clean_tags(note.get("tags") or [])
    fname = sanitize(title) + ".md"
    dest = NOTES_DIR / fname
    if dest.exists():
        return None  # keep existing note; don't clobber
    near = similar_existing_note(title, body, tags)
    sim_line = ""
    if near:
        sim_line = f"similar_to: \"[[{near[0]}]]\"\nsimilarity: {near[1]:.2f}\n"
        print(f"  ! near-duplicate of an existing note ({near[1]:.0%}): {near[0][:60]}")
    tag_lines = "\n".join(f"  - {t}" for t in (["claude/extracted"] + tags))
    # How this claim decays. Notes carrying operational state read exactly like notes carrying
    # principles, so a vault silently accumulates confident statements that stopped being true
    # months ago — in the vault this came from, notes asserting "UNCOMMITTED" were wrong by the time anyone
    # relied on them. Recording shelf life at write time is the only cheap moment to do it;
    # nobody classifies 954 notes later. "unknown" when the model declines to choose, so the
    # gap stays visible instead of defaulting into a lie.
    vol = as_text(note.get("volatility")).lower()
    if vol not in ("timeless", "dated", "live"):
        vol = "unknown"
    content = (
        "---\n"
        "type: note\n"
        f"created: {date}\n"
        f"volatility: {vol}\n"
        f"source: \"[[{conversation_link(src)}]]\"\n"
        # Which version of the conversation this came from, so another machine (whose checkpoint
        # is its own) can tell it was already extracted. See source_index().
        + (f"source_hash: {sig}\n" if sig else "")
        + f"{sim_line}"
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

    if not hold_lock("extract_notes.lock"):
        print("extract_notes: another extraction run holds the lock — skipping this one")
        return
    state = open_state(load_state, STATE_FILE, args.force)
    by_source = source_index(NOTES_DIR)
    ledger = Ledger("notes")
    processed = 0
    failed: list[str] = []
    total_notes = 0
    adopted = False
    for src in sources:
        key = str(src.relative_to(VAULT))
        st = src.stat()
        stat_sig = f"{st.st_mtime_ns}:{st.st_size}"
        prev = state.get(key, {})
        if not args.force and prev.get("stat") == stat_sig:
            continue                     # untouched since it was checked: skip without reading it
        raw = src.read_bytes()
        transcript = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        # strip the frontmatter of the conversation note before sending
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
        date = re.search(r"\d{4}-\d{2}-\d{2}", src.name)
        date = date.group(0) if date else datetime.now().strftime("%Y-%m-%d")
        try:
            notes = extract_notes_from(transcript, args.model, [n for n, _ in from_here])
            # Inside the try: one bad item (an unwritable name, an odd field) fails this
            # conversation, reported and retried, instead of killing the run before any checkpoint.
            written = [w for n in notes if (w := write_atomic_note(n, src, date, args.dry_run, sig))]
        except Exception as e:
            print(f"  ! skipped ({e})")
            failed.append(src.name)      # not recorded in state, so the next run retries it
            continue
        total_notes += len(written)
        processed += 1
        if not args.dry_run:
            by_source.setdefault(src.stem, []).extend((Path(w).stem, sig) for w in written)
            state[key] = {"sig": sig, "stat": stat_sig,
                          "notes": list(dict.fromkeys([*prev.get("notes", []), *written]))}
            save_state(state)
            ledger.record(src.stem, sig)
        if not notes:
            print("  (no durable insights)")
    if adopted:
        save_state(state)

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
