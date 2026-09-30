"""Helpers for the regression scripts in tests/: throwaway git vaults with a copy of tools/.

Every test builds its own vault under a temp dir, copies the tools it exercises, and runs them
as subprocesses exactly as the git hook or the sync would. Nothing touches the real vault, no
network, no model calls. Set PALIMPSEST_TOOLS to test a different tools/ tree (e.g. a checkout
of main, to confirm a test fails before its fix).

Every vault and dir made here (make_vault, tempdir) is removed when the test script exits; set
PALIMPSEST_KEEP_TMP=1 to keep them for a post-mortem.
"""
from __future__ import annotations
import atexit, os, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS_SRC = Path(os.environ.get("PALIMPSEST_TOOLS", REPO / "tools"))
_MADE: list[Path] = []


def _writable_retry(func, path, _exc) -> None:
    """git makes its object files read-only, which Windows refuses to delete."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _cleanup() -> None:
    if os.environ.get("PALIMPSEST_KEEP_TMP"):
        return
    for d in reversed(_MADE):
        if sys.version_info >= (3, 12):
            shutil.rmtree(d, onexc=_writable_retry)
        else:
            shutil.rmtree(d, onerror=_writable_retry)


atexit.register(_cleanup)


def tempdir(prefix: str = "palimpsest-test-") -> Path:
    """A fresh temp dir, removed when the test script exits."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    _MADE.append(d)
    return d


def make_vault(with_hooks: bool = False) -> Path:
    v = tempdir()
    shutil.copytree(TOOLS_SRC, v / "tools", ignore=shutil.ignore_patterns("cache", "__pycache__", "*.pyc"))
    git(v, "init", "-q", "-b", "main")
    git(v, "config", "user.name", "test")
    git(v, "config", "user.email", "test@example.invalid")
    if with_hooks:
        git(v, "config", "core.hooksPath", "tools/githooks")
    return v


def git(v: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=v, capture_output=True, text=True, check=check)


def run(v: Path, script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(v / "tools" / script), *args], cwd=v, capture_output=True,
                          text=True, env={**os.environ, **(env or {})})


def write(v: Path, rel: str, text: str) -> Path:
    p = v / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class Checks:
    def __init__(self, name: str):
        self.name, self.failed, self.n = name, 0, 0

    def ok(self, cond: bool, what: str, detail: str = ""):
        self.n += 1
        if not cond:
            self.failed += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {what}" + (f"\n        {detail}" if detail and not cond else ""))

    def done(self) -> int:
        print(f"{self.name}: {self.n - self.failed}/{self.n} passed")
        return 1 if self.failed else 0
