"""The commit guards must actually run on a fresh clone, on every OS.

tools/githooks/pre-commit was committed as 100644 (not executable). git on macOS and Linux
SKIPS a non-executable hook with only a hint — so on every non-Windows clone since the first
release, `git config core.hooksPath tools/githooks` enabled guards that never ran, and a
credential committed straight through. Found by a clean-install test, 2026-09.

This clones the COMMITTED repo (so file modes are what a user gets) and commits a synthetic key.
Set PALIMPSEST_REF to test another ref (e.g. main, to see it fail).
"""
import os, subprocess, sys, tempfile
from pathlib import Path
from _util import Checks, REPO

FAKE_KEY = "AKIA" + "QZXW" * 4


def main() -> int:
    c = Checks("git hook runs on a fresh clone")
    ref = os.environ.get("PALIMPSEST_REF") or subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    mode = subprocess.run(["git", "ls-tree", ref, "tools/githooks/pre-commit"], cwd=REPO,
                          capture_output=True, text=True).stdout.split()[:1]
    c.ok(mode == ["100755"], f"pre-commit is committed executable on {ref}", str(mode))
    if os.name == "nt":
        print("  (Windows ignores the executable bit; the clone check below is for macOS/Linux)")
        return c.done()
    d = Path(tempfile.mkdtemp(prefix="palimpsest-clone-"))
    subprocess.run(["git", "clone", "-q", "-b", ref, str(REPO), str(d)], check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@example.invalid"), ("core.hooksPath", "tools/githooks")):
        subprocess.run(["git", "config", k, v], cwd=d, check=True)
    (d / "10 Notes").mkdir(exist_ok=True)
    (d / "10 Notes" / "leak.md").write_text(f"key {FAKE_KEY}\n")
    subprocess.run(["git", "add", "10 Notes/leak.md"], cwd=d, check=True)
    r = subprocess.run(["git", "commit", "-m", "leak"], cwd=d, capture_output=True, text=True)
    c.ok(r.returncode != 0 and "secret-scan" in (r.stdout + r.stderr),
         "a commit carrying a key is BLOCKED by the hook on a fresh clone", (r.stdout + r.stderr)[-400:])
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
