"""Regression for the 2026-10-02 review: setup.py's line-ending repair rewrote every note.

306df00 added `* text=auto eol=lf`, and fix_line_endings picked its files by "eol=lf" in
`git ls-files --eol`, so every note sitting CRLF in a core.autocrlf=true working tree matched and
was rewritten on the next setup.py run. git treats a CRLF working copy of an LF blob as clean, so
that gained nothing, and it changed each note's raw bytes — what embed.py hashes — so the next
hybrid ask re-embedded the whole vault (hours on a laptop CPU). The repair is for what an
interpreter reads: only files the rules pin with an explicit `text eol=lf` (the hook, *.sh).

The vault carries this repo's .gitattributes and is put in the state an older checkout leaves:
CRLF files whose blobs did not change when the rules arrived, so git never rewrote them and the
tree is clean.

1. the stale state is real: hook, script and note sit CRLF, git reads the note as
   `text=auto eol=lf` and the hook as `text eol=lf`, and the tree is clean
2. a CRLF hook and a CRLF *.sh are rewritten with LF (the hook stays executable)
3. a CRLF note is left byte-identical, and setup does not report it
4. the tree is clean afterwards (tracked files: running setup.py leaves an untracked __pycache__)

Check 3 fails with PALIMPSEST_TOOLS pointed at tools/ from 60b4bff.
"""
import os, shutil, sys
from pathlib import Path
from _util import Checks, REPO, git, make_vault, run, write

HOOK = "tools/githooks/pre-commit"
SCRIPT = "launch.sh"
NOTE = "10 Notes/A note.md"


def eol(v: Path, path: str) -> str:
    """git's own verdict for one path, e.g. 'i/lf w/crlf attr/text=auto eol=lf'."""
    return " ".join(git(v, "ls-files", "--eol", "--", path).stdout.partition("\t")[0].split())


def main() -> int:
    c = Checks("review 2026-10-02 — setup.py repairs only what an interpreter reads")
    v = make_vault()
    shutil.copyfile(REPO / ".gitattributes", v / ".gitattributes")
    write(v, NOTE, "# A note\n\nOne idea, linked to [[Another note]].\n")
    write(v, SCRIPT, "#!/bin/sh\necho ok\n")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "seed")
    git(v, "config", "core.autocrlf", "true")
    # An older checkout, reproduced: info/attributes outranks .gitattributes, so while it says CRLF
    # the checkout writes CRLF files and the index records their stat. Once it is gone the vault's
    # own rules apply to blobs that did not change, so git never rewrites the files.
    info = v / ".git" / "info" / "attributes"
    info.parent.mkdir(exist_ok=True)
    info.write_text("* text eol=crlf\n")
    for p in (HOOK, SCRIPT, NOTE):
        (v / p).unlink()                       # else the unchanged blob is not rewritten
    git(v, "checkout", "--", HOOK, SCRIPT, NOTE)
    info.unlink()

    note = (v / NOTE).read_bytes()
    stale = [p for p in (HOOK, SCRIPT, NOTE) if b"\r\n" in (v / p).read_bytes()]
    attrs = {p: eol(v, p) for p in (HOOK, SCRIPT, NOTE)}
    dirty = git(v, "status", "--short", "--untracked-files=no").stdout
    c.ok(len(stale) == 3 and attrs[NOTE].endswith("attr/text=auto eol=lf")
         and attrs[HOOK].endswith("attr/text eol=lf") and attrs[SCRIPT].endswith("attr/text eol=lf")
         and not dirty,
         "an older checkout's state: CRLF hook, script and note under the eol rules, tree clean",
         f"CRLF in {stale}; {attrs}; status={dirty!r}")

    r = run(v, "setup.py", "--fix-line-endings")
    out = r.stdout + r.stderr
    hook = v / HOOK
    c.ok(r.returncode == 0 and b"\r" not in hook.read_bytes() and b"\r" not in (v / SCRIPT).read_bytes()
         and HOOK in out and SCRIPT in out and (os.name == "nt" or os.access(hook, os.X_OK)),
         "a CRLF hook and a CRLF *.sh are rewritten with LF (text eol=lf), the hook stays executable",
         f"exit {r.returncode}; {eol(v, HOOK)}; {eol(v, SCRIPT)}\n{out[-400:]}")
    after = (v / NOTE).read_bytes()
    kept = b"\r\n" in after
    c.ok(after == note and NOTE not in out,
         "a CRLF note (text=auto eol=lf) is left byte-identical, so embed.py's hash of it holds",
         f"{eol(v, NOTE)}; CRLF kept={kept}\n{out[-400:]}")
    status = git(v, "status", "--short", "--untracked-files=no").stdout
    c.ok(not status, "the tree stays clean after the repair", f"status={status!r}")
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
