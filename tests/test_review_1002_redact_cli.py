"""redact.py's CLI (`python tools/redact.py < note`, the manual scrub) reads and writes its own bytes.

Review of 2026-10-02. The scanner tests ran the CLI with PYTHONIOENCODING=utf-8, which took away the
precondition of the bug e232e70 fixed (stdin read in the Windows code page, so a Cyrillic term went
through unredacted), and their "term not in output" checks also passed on mojibake: importing
scan_secrets turns stdout to UTF-8, so a stdin read in cp1251 came back out as UTF-8 mojibake, not
as the input. Every run here sets PYTHONIOENCODING=cp1251, so a text stdin is the code page as on a
stock Windows, and compares the output byte for byte.

 1. A UTF-8 note with a Cyrillic deny-listed term comes out exactly as itself with the term
    replaced, and the CLI reports 1 substitution.
 2. LF stays LF, CRLF stays CRLF, and a note mixing them keeps both.

Set PALIMPSEST_TOOLS to a tools/ tree from before e232e70 to see these fail.
"""
import os, subprocess, sys
import _util
from _util import Checks

TERM = "Сидорова"                                       # synthetic deny-listed surname, Cyrillic
NAME = "Zorbanek"                                       # synthetic, Latin
MARK = "[redacted]"


def cli(v, data: bytes):
    """(exit code, stdout bytes, stderr) of the CLI fed `data`, with a cp1251 stdio."""
    r = subprocess.run([sys.executable, str(v / "tools" / "redact.py")], cwd=v, input=data, capture_output=True,
                       env={**os.environ, "PYTHONIOENCODING": "cp1251"})
    return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")


def main() -> int:
    c = Checks("review 2026-10-02: redact CLI")
    v = _util.make_vault()
    (v / "tools" / ".redact_terms.txt").write_bytes(f"{TERM}\n{NAME}\n".encode("utf-8"))

    # 1. UTF-8 in under a cp1251 stdio: the exact bytes out, and one substitution
    rc, out, err = cli(v, f"met {TERM} today\n".encode("utf-8"))
    want = f"met {MARK} today\n".encode("utf-8")
    c.ok(rc == 0 and out == want and "redact: 1 substitution(s)" in err,
         "a Cyrillic deny-listed term in UTF-8 is redacted under a cp1251 stdio, output byte-exact",
         f"rc={rc} out={out!r} want={want!r} err={err!r}")

    # 2. newlines are bytes like any other
    bad = []
    for name, a, b in (("LF", "\n", "\n"), ("CRLF", "\r\n", "\r\n"), ("mixed", "\n", "\r\n")):
        rc, out, err = cli(v, f"met {TERM}{a}today{b}end{a}".encode("utf-8"))
        want = f"met {MARK}{a}today{b}end{a}".encode("utf-8")
        if rc != 0 or out != want or "redact: 1 substitution(s)" not in err:
            bad.append((name, rc, out, want, err))
    c.ok(not bad, "LF in gives LF out, CRLF in gives CRLF out, a mix stays mixed, byte-exact", repr(bad))

    return c.done()


if __name__ == "__main__":
    sys.exit(main())
