"""Helpers for the regression scripts in tests/: throwaway git vaults with a copy of tools/.

Every test builds its own vault under a temp dir, copies the tools it exercises, and runs them
as subprocesses exactly as the git hook or the sync would. Nothing touches the real vault, no
network, no model calls. Set PALIMPSEST_TOOLS to test a different tools/ tree (e.g. a checkout
of main, to confirm a test fails before its fix).
"""
from __future__ import annotations
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS_SRC = Path(os.environ.get("PALIMPSEST_TOOLS", REPO / "tools"))


def make_vault(with_hooks: bool = False) -> Path:
    v = Path(tempfile.mkdtemp(prefix="palimpsest-test-"))
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
