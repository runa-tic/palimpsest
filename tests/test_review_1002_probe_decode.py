"""Regression checks for the 2026-10-02 review of state.py's command probes: their output is decoded
strictly in the probe's declared "encoding", never as UTF-8 with errors="replace".

Under the lossy decode every letter a cp1251 probe printed became U+FFFD, so two values of one length
were the same string and a service going down was logged as "1 unchanged".

1. cp1251 output with no "encoding" key records "probe error (output not utf-8)", with the raw
   bytes in hex in the detail, and no U+FFFD reaches the ledger.
2. with "encoding": "cp1251" the same output records the right text.
3. two different cp1251 values of one length are two values, not "1 unchanged".
4. the default stays UTF-8: UTF-8 output with no key records the text (a guard; it held before).
5. a failed run's detail is decoded in the declared encoding too.
6. an "encoding" with no codec is a probe error on this machine.

Each probe is a `python -c` that writes fixed bytes to its binary stdout, so what it prints does not
depend on the locale or code page the test runs under. Every probe has "network": false: no check
here contacts the network control hosts.
"""
import json, sys
from _util import Checks, make_vault, git, run, write

UP, DOWN, FAIL = "работает", "отключен", "сбой"   # UP and DOWN: same length, both in cp1251


def st(v, *args):
    return run(v, "state.py", *args, env={"PALIMPSEST_MACHINE": "alpha"})


def emit(data: bytes, stream: str = "stdout", rc: int = 0) -> list[str]:
    # The bytes travel as their ASCII repr, so no argv or console encoding touches them.
    return [sys.executable, "-c", f"import sys; sys.{stream}.buffer.write({data!r}); sys.exit({rc})"]


def probe(v, cmd: list[str], **extra):
    write(v, "palimpsest.json", json.dumps({"version": 1, "probes": [
        {"name": "api", "entity": "my-api", "attr": "status", "cmd": cmd, "network": False, **extra}]}))
    return st(v, "probe", "--only", "api")


def detail(v) -> str:
    obs = json.loads((v / "State" / ".observed.json").read_text(encoding="utf-8"))
    return str((obs.get("detail:api") or {}).get("detail", ""))


def main() -> int:
    c = Checks("review 2026-10-02: probe output decode")
    v = make_vault()
    write(v, ".gitignore", "__pycache__/\nState/Register.md\nState/current.json\nState/.observed.json\n")
    write(v, "10 Notes/x.md", "x\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    st(v, "register", "my-api", "--kind", "service")

    up = UP.encode("cp1251") + b"\n"
    r = probe(v, emit(up))
    c.ok("my-api.status = probe error (output not utf-8)" in r.stdout,
         "1. cp1251 output with no encoding key records a probe error", r.stdout + r.stderr)
    c.ok(up[:4].hex(" ") in detail(v), "1. ...and keeps the raw bytes' hex prefix in the detail", detail(v))
    facts = (v / "State" / "facts.jsonl").read_text(encoding="utf-8")
    c.ok("�" not in facts, "1. ...and no U+FFFD reaches the ledger", ascii(facts[-300:]))

    r = probe(v, emit(up), encoding="cp1251")
    c.ok(f"my-api.status = {UP}" in r.stdout, "2. with encoding cp1251 the same output records the right text",
         r.stdout + r.stderr)

    r = probe(v, emit(DOWN.encode("cp1251") + b"\n"), encoding="cp1251")
    c.ok(f"my-api.status = {DOWN}  (was {UP})" in r.stdout and "1 new fact(s), 0 unchanged" in r.stdout,
         "3. two different cp1251 values of one length do not collide (it said '1 unchanged')",
         r.stdout + r.stderr)

    r = probe(v, emit(UP.encode("utf-8") + b"\n"))
    c.ok(f"my-api.status = {UP}" in r.stdout, "4. UTF-8 output with no encoding key still records the text",
         r.stdout + r.stderr)

    r = probe(v, emit(FAIL.encode("cp1251") + b"\n", stream="stderr", rc=1), encoding="cp1251")
    c.ok("my-api.status = unreachable (rc 1)" in r.stdout and FAIL in detail(v),
         "5. a failed run's detail is decoded in the declared encoding", r.stdout + " | detail: " + detail(v))

    r = probe(v, emit(b"up\n"), encoding="no-such-codec")
    c.ok("my-api.status = probe error (LookupError)" in r.stdout,
         "6. an encoding with no codec is recorded as a probe error", r.stdout + r.stderr)

    r = probe(v, emit(b"active\n" + "\u0441\u043b\u0443\u0436\u0431\u0430 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442\n".encode("cp1251")))
    c.ok("my-api.status = active" in r.stdout,
         "7. only the recorded first line has to decode: later lines in a code page do not make it an error",
         r.stdout + r.stderr)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
