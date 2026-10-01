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
os.environ["PALIMPSEST_STUB_PY"] = sys.executable      # read by the Windows stub launchers


def _writable_retry(func, path, _exc) -> None:
    """git makes its object files read-only, which Windows refuses to delete."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(d, ignore_errors: bool = True) -> None:
    """shutil.rmtree that also removes git's read-only object and pack files on Windows."""
    if not os.path.lexists(d):
        return
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(d, onexc=_writable_retry)
        else:
            shutil.rmtree(d, onerror=_writable_retry)
    except OSError:
        if not ignore_errors:
            raise


def _cleanup() -> None:
    if os.environ.get("PALIMPSEST_KEEP_TMP"):
        return
    for d in reversed(_MADE):
        rmtree(d)


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


_STUBS: dict[Path, set[str]] = {}


def stub(bindir: Path, name: str, source: str) -> Path:
    """A fake command `name` in bindir that runs the Python `source` with this interpreter.

    POSIX runs one file with a shebang. Windows cannot exec a shebang file and finds commands
    through PATHEXT, so there the stub is name.py plus a name.cmd launcher, which is also how the
    tools resolve a real npm `claude.cmd`. Returns what shutil.which would return for it."""
    bindir = Path(bindir)
    bindir.mkdir(parents=True, exist_ok=True)
    _STUBS.setdefault(bindir.resolve(), set()).add(name)
    if os.name == "nt":
        (bindir / f"{name}.py").write_text(source, encoding="utf-8")
        exe = bindir / f"{name}.cmd"
        # The interpreter comes from the environment, not a literal: cmd reads a batch file in the
        # OEM codepage, so a non-ASCII path in it (a Cyrillic user profile) would be mangled.
        exe.write_text(f'@"%PALIMPSEST_STUB_PY%" "%~dp0{name}.py" %*\r\n', encoding="ascii")
        return exe
    exe = bindir / name
    exe.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def stub_path(bindir: Path) -> str:
    """PATH with bindir first, refusing to run when a stubbed name resolves anywhere else.

    The fake must win, or the test sends its content to the real binary: on Windows a stub that
    PATHEXT cannot see silently made the real claude.exe answer (first Windows run, 2026-10-01)."""
    path = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
    for name in _STUBS.get(Path(bindir).resolve(), ()):
        found = shutil.which(name, path=path)
        if not found or Path(found).resolve().parent != Path(bindir).resolve():
            raise SystemExit(f"test harness: '{name}' resolves to {found!r}, not the stub in {bindir}; "
                             "refusing to run so no real command gets test content")
    return path


def can_symlink() -> bool:
    """Whether this process may create a file symlink (Windows needs admin or Developer Mode)."""
    global _CAN_SYMLINK
    if _CAN_SYMLINK is None:
        d = tempdir("palimpsest-symlink-probe-")
        try:
            (d / "t").write_text("x")
            os.symlink(d / "t", d / "l")
            _CAN_SYMLINK = True
        except (OSError, NotImplementedError):
            _CAN_SYMLINK = False
    return _CAN_SYMLINK


_CAN_SYMLINK: bool | None = None


def write(v: Path, rel: str, text: str) -> Path:
    p = v / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class Checks:
    def __init__(self, name: str):
        self.name, self.failed, self.n, self.skipped = name, 0, 0, 0

    def ok(self, cond: bool, what: str, detail: str = ""):
        self.n += 1
        if not cond:
            self.failed += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {what}" + (f"\n        {detail}" if detail and not cond else ""))

    def skip(self, what: str, why: str):
        """A check this platform cannot run: reported, never counted as passed."""
        self.skipped += 1
        print(f"  SKIP  {what}\n        ({why})")

    def done(self) -> int:
        print(f"{self.name}: {self.n - self.failed}/{self.n} passed"
              + (f", {self.skipped} skipped on this platform" if self.skipped else ""))
        return 1 if self.failed else 0
