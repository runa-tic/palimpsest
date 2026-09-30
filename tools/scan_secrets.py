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
import codecs, re, sys, subprocess
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parent     # was VAULT / "_tools": the allow list was never read
VAULT = TOOLS.parent
ALLOW_FILE = TOOLS / ".secret_scan_allow.txt"

# Directories the --all / directory walk never enters (protected, binary-heavy, or slow). Staged
# files are scanned wherever they are: Obsidian AI plugins keep API keys in
# .obsidian/plugins/<p>/data.json, and a text file in _media is still text (review, 2026-09-30).
SKIP_DIRS = {".git", "_media", "node_modules", ".obsidian", "__pycache__"}
# Specific repo-relative paths never scanned, matched exactly. A prefix match exempted anything
# starting "tools/.secrets" (.secrets.env, .secrets_backup), and nothing gitignores those, so a
# credentials file was committed "clean"; a secrets file is exactly what should block.
SKIP_PATHS = {
    "tools/scan_secrets.py",
    "tools/.secret_scan_allow.txt",
    "tools/.redact_terms.txt",
}

# (label, compiled regex). These shapes are high-confidence credentials.
HIGH = [
    ("CoinGecko API key",   re.compile(r"CG-[A-Za-z0-9]{20,}")),
    # Current keys (sk-proj-, sk-svcacct-, sk-admin-) have a base64url body with '_' and '-', so
    # the alphanumeric-only class missed about half of them and cut the rest short, leaving the
    # tail in the note (review, 2026-09-30). The lookbehind keeps prose like "risk-admin-..." out,
    # but lets a key follow a JSON escape ("\nsk-proj-..." in tool output), which it had rejected.
    ("OpenAI key",          re.compile(r"(?:(?<![A-Za-z0-9])|(?<=\\[nrtbf])|(?<=\\u[0-9A-Fa-f]{4}))"
                                       r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
                                       r"|sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("Anthropic key",       re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token",        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}")),
    ("Google API key",      re.compile(r"AIza[A-Za-z0-9_-]{30,}")),
    ("Slack token",         re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key id",   re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    # The secret half has no prefix, only its label. Without this rule redaction masked the id and
    # left a working secret key, which also removed the one thing the guard would have blocked on.
    # The separator admits '\' for the JSON-escaped form ({\"SecretAccessKey\": \"...\"}); group "v"
    # is the value, which redact.py masks without eating the label.
    ("AWS secret access key", re.compile(
        r"(?i)(?:aws_secret_access_key|secretaccesskey)['\"\\\s:=]+(?P<v>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")),
    # Lookarounds, not \b: in a Bot API URL (.../bot<token>/getMe) "bot" runs straight into the
    # digits, and a secret may end in '-'; \b missed both.
    ("Telegram bot token",  re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("Private key block",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),   # BLOCK: PGP armor
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


def safe_label(rel: str) -> str:
    """A path as printed: every credential shape in it masked. The path is content (notes are named
    after conversations), and this output is recorded into the vault and copied into vault_push's
    log, so no line may carry a name raw, a content finding's label included."""
    for rules in (HIGH, WARN):
        for _, rx in rules:
            rel = rx.sub(lambda m: mask(m.group(0)), rel)
    return rel


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


_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),   # before UTF-16: same lead
          (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _encoding(head: bytes) -> str:
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return enc
    return "utf-8"


def decode(raw: bytes) -> str:
    """File bytes as scannable text; never fails. Windows PowerShell 5 writes UTF-16 (`>`,
    Out-File), which read as UTF-8 put a NUL between every character so no pattern matched, and
    a strict UTF-8 read dropped cp1251/latin-1 files from --all without a word (review, 2026-09-30)."""
    enc = _encoding(raw)
    text = raw.decode(enc, errors="replace")
    # NULs left in a UTF-8 read are UTF-16 without a BOM (or appended to a UTF-8 file by `>>`):
    # dropping them joins the characters back up.
    return text.replace("\x00", "") if enc == "utf-8" else text


# Size policy, the same in every mode. Up to MAX_BYTES a file is read whole and scanned, binary or
# not. Over it, only its first HEAD bytes are read to tell binary from text: a binary (a video in
# _media) is listed as NOT scanned and does not fail, since reading, decoding and running every
# rule over a 60MB video took ~15x its size in RAM in the hook (review of fix/scanners,
# 2026-09-30). A large TEXT file is still scanned, in CHUNK-sized runs of whole lines, so memory
# stays flat: an earlier cap skipped it outright, and a 5.8MB session transcript with a key in it
# went through the unattended push, whose log shows guard output only when a commit fails (second
# review, 2026-09-30). Text over TEXT_MAX (about 4s of scanning) is not scanned and FAILS.
MAX_BYTES = 5_000_000
TEXT_MAX = 64_000_000
HEAD = 8192
# Extensions a large binary may carry and still be skipped (see text_blocks). Deliberately media and
# archives only: a large .bin/.db/.log with a binary-looking head is scanned (or fails), not skipped.
MEDIA_SUFFIXES = frozenset(".png .jpg .jpeg .gif .webp .heic .heif .tif .tiff .bmp .ico .pdf .mp4 .mov .m4v "
                           ".mkv .webm .avi .mp3 .m4a .wav .flac .ogg .opus .aac .zip .7z .rar .gz .tgz .bz2 "
                           ".xz .dmg .iso .psd .ai .sketch .fig .epub .docx .xlsx .pptx .key .pages .numbers".split())
CHUNK = 1 << 20

# Leading bytes of media and archive formats. A match marks a large file binary whatever its bytes.
_MAGIC = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04",
          b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"OggS", b"fLaC", b"ID3", b"RIFF",
          b"\x1aE\xdf\xa3", b"SQLite format 3\x00", b"wOFF", b"wOF2", b"\x00\x00\x01\x00")


def _utf16_text(head: bytes) -> bool:
    """BOM-less UTF-16: a NUL in every other byte for ASCII. Read in either order, it gives no
    replacement or control characters (random bytes give a replacement for ~6% of units)."""
    even = head[:len(head) & ~1]
    for enc in ("utf-16-le", "utf-16-be"):
        t = even.decode(enc, errors="replace")
        bad = sum(c == "\ufffd" or (c < " " and c not in "\t\n\r") for c in t)
        if bad <= len(t) // 100:
            return True
    return False


def is_binary(head: bytes) -> bool:
    """Whether a file whose first bytes are `head` is binary (not scanned when large) rather than
    text (scanned at any size up to TEXT_MAX). A BOM means text. A media signature means binary
    when a control byte backs it up: "ID3" or "RIFF" can open a note. Otherwise NULs that are not
    UTF-16, or more than 3% control bytes (random data has ~10%). The costly mistake is text taken
    for binary, which is skipped; binary taken for text is scanned, or fails over TEXT_MAX."""
    if any(head.startswith(bom) for bom, _ in _BOMS):
        return False
    ctrl = sum(c < 9 or (13 < c < 32 and c != 27) or c == 127 for c in head)
    if ctrl and (head.startswith(_MAGIC) or head[4:8] == b"ftyp"):    # ftyp: mp4 / mov / m4a / heic
        return True
    if b"\x00" in head:
        return not _utf16_text(head)
    return ctrl * 32 > len(head)


class NotScanned(Exception):
    """A file that was not scanned. `fails`: the scan must not report clean over it."""
    def __init__(self, why: str, fails: bool):
        super().__init__(why)
        self.why, self.fails = why, fails


def _read_n(read, n: int) -> bytes:
    out = b""
    while len(out) < n:
        b = read(n - len(out))
        if not b:
            break
        out += b
    return out


def text_blocks(read, size: int, path: str = ""):
    """(first line number, text of whole lines) runs of a file of `size` bytes that `read(n)`
    returns in order. Raises NotScanned (before yielding) for a large binary or oversize text."""
    head = _read_n(read, min(size, HEAD))
    if size > MAX_BYTES:
        # Skipped only when the NAME and the BYTES agree it is media: a head that merely looks
        # binary (a NUL-padded log, a control-heavy typescript) used to be skipped and pushed
        # unscanned by the nightly run, which shows guard output only on a failed commit
        # (review, 2026-09-30). Anything else is scanned as text, or fails over TEXT_MAX.
        if is_binary(head) and Path(path).suffix.lower() in MEDIA_SUFFIXES:
            raise NotScanned("binary over 5MB", fails=False)
        if size > TEXT_MAX:
            raise NotScanned(f"text over {TEXT_MAX // 1_000_000}MB, too large to scan", fails=True)
    if size <= MAX_BYTES:
        yield 1, decode(head + _read_n(read, size - len(head)))
        return
    enc = _encoding(head)
    dec = codecs.getincrementaldecoder(enc)(errors="replace")
    line, carry, left, data = 1, "", size - len(head), head
    while True:
        final = not left
        text = carry + dec.decode(data, final)
        if enc == "utf-8":
            text = text.replace("\x00", "")
        cut = len(text) if final else text.rfind("\n") + 1
        block, carry = text[:cut], text[cut:]
        if block:
            yield line, block
            line += len(block.splitlines())
        if final:
            return
        data = _read_n(read, min(CHUNK, left))
        left -= len(data)
        if not data:
            left = 0


def file_blocks(p: Path):
    """text_blocks of a file on disk; NotScanned if it cannot be read."""
    try:
        with open(p, "rb") as f:
            yield from text_blocks(f.read, p.stat().st_size, str(p))
    except OSError as e:
        raise NotScanned(f"unreadable ({type(e).__name__})", fails=True) from None


def _not_scanned(why: str, fails: bool):
    raise NotScanned(why, fails)
    yield       # a generator, like text_blocks: raises when iterated


def staged_blobs(paths: list[str]):
    """(path, text_blocks of its staged version) for each path, in order, from two git processes
    in all: `git ls-files -s` for the blob ids and one `git cat-file --batch` for the contents. A
    `git cat-file -s` and a `git show` per file had doubled the hook's time (review, 2026-09-30),
    and process spawns cost more on the Windows box. Consume each file's blocks before asking for
    the next: the contents stream through the one pipe."""
    ls = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=VAULT, capture_output=True)
    index: dict[str, tuple[str, str]] = {}
    for rec in ls.stdout.split(b"\0"):
        meta, tab, name = rec.partition(b"\t")
        if tab:
            mode, oid, stage = meta.decode().split()
            index[name.decode("utf-8", errors="replace")] = (mode, oid)
    cat = None
    try:
        for path in paths:
            mode, oid = index.get(path, ("", ""))
            if not oid or mode == "160000":         # not in the index / a submodule commit
                yield path, _not_scanned("not in the index" if not oid else "submodule", fails=not oid)
                continue
            if cat is None:
                cat = subprocess.Popen(["git", "cat-file", "--batch"], cwd=VAULT,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            cat.stdin.write(oid.encode() + b"\n")
            cat.stdin.flush()
            header = cat.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                yield path, _not_scanned("unreadable from the index", fails=True)
                continue
            left = [int(header[2])]

            def read(n: int, left=left) -> bytes:
                b = cat.stdout.read(min(n, left[0]))
                left[0] -= len(b)
                return b
            yield path, text_blocks(read, left[0], path)
            while left[0] and read(CHUNK):          # the rest of a file not (fully) consumed
                pass
            cat.stdout.read(1)                        # the newline after each object
    finally:
        if cat is not None:
            cat.stdin.close()
            cat.stdout.close()
            cat.wait()


def is_skipped(rel: str, walk: bool = True) -> bool:
    rel = rel.replace("\\", "/")
    if walk and any(d in SKIP_DIRS for d in rel.split("/")):
        return True
    return rel in SKIP_PATHS


def walk_files(root: Path):
    """(label, path) of every file under root the walk does not skip."""
    for p in root.rglob("*"):
        if p.is_file():
            fr = p.resolve()    # the check and the relative name must use the same path:
            rel = str(fr.relative_to(VAULT)) if VAULT in fr.parents else str(fr)  # a relative dir arg crashed
            if not is_skipped(rel):
                yield rel, p


def scan_blocks(blocks, allow: list[str]) -> list[tuple]:
    """scan_text over text_blocks, with line numbers counted from the start of the file."""
    return [(sev, label, first - 1 + ln, masked)
            for first, text in blocks for sev, label, ln, masked in scan_text(text, allow)]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    strict = "--strict" in args
    args = [a for a in args if a != "--strict"]

    allow = load_allow()
    skipped: list[tuple[str, str]] = []   # (file, why) not scanned: listed, never counted as clean
    failed = False                 # a file that was not scanned and must not pass as clean

    def targets():
        """(label, text_blocks, named) for every file this run covers, in order."""
        if "--all" in args:
            for p in VAULT.rglob("*"):
                if p.is_file() and not is_skipped(rel := str(p.relative_to(VAULT))):
                    yield rel, file_blocks(p), False
        elif args:
            for a in args:
                p = Path(a)
                if p.is_dir():
                    for rel, f in walk_files(p):
                        yield rel, file_blocks(f), False
                else:          # a file named on the command line that is not scanned fails
                    yield a, (file_blocks(p) if p.is_file() else _not_scanned("missing", True)), True
        else:
            wanted = [rel for rel in staged_files() if not is_skipped(rel, walk=False)]
            for rel, blocks in staged_blobs(wanted):
                # The name is content too: import_claude.py names files after the conversation.
                yield f"{rel} [path]", [(1, rel)], False
                yield rel, blocks, False

    high, warn, n = [], [], 0
    for rel, blocks, named in targets():
        try:
            found = scan_blocks(blocks, allow)
        except NotScanned as e:
            skipped.append((rel, e.why))
            failed |= e.fails or named
            continue
        n += not rel.endswith(" [path]")
        rel = safe_label(rel)
        for sev, label, lineno, masked in found:
            (high if sev == "HIGH" else warn).append((rel, label, lineno, masked))
    mode = "whole vault" if "--all" in args else f"{n} path(s)" if args else "staged changes"

    if skipped:
        print(f"secret-scan: {len(skipped)} file(s) NOT scanned:")
        for rel, why in skipped:
            print(f"  [skip] {safe_label(rel)}  ({why})")
    if failed:
        print("secret-scan: FAILED — a file above could not be scanned, so this is not a clean result."
              " Split or remove it, or scan it by hand.")
    if not high and not warn:
        if not failed:
            print(f"secret-scan: clean ({mode}{'; see NOT scanned above' if skipped else ''}).")
        return 1 if failed else 0

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
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
