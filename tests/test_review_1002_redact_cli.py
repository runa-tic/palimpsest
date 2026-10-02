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
 3. Input with a BOM (UTF-8, UTF-16 LE/BE, UTF-32 LE/BE) is redacted and written back in its own
    encoding, BOM included: PowerShell 5.1 `>` writes UTF-16 LE, which went through unredacted with
    "0 substitution(s)" and exit 0.
 4. Input that is not UTF-8 and has no BOM (cp1251, a stray byte, UTF-16 without a BOM, UTF-16
    appended by `>>`), or that its BOM misdescribes, exits 2 and writes nothing: no mangled copy,
    no unredacted one.

Set PALIMPSEST_TOOLS to a tools/ tree from before e232e70 (1, 2) or from before BOM handling (3, 4)
to see these fail.
"""
import codecs, os, subprocess, sys
import _util
from _util import Checks

TERM = "Сидорова"                                       # synthetic deny-listed surname, Cyrillic
NAME = "Zorbanek"                                       # synthetic, Latin
AKIA = "AKIA" + "QZXW" * 4                              # synthetic, AWS-shaped
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

    # 3. a BOM names the encoding, and the output goes back in it, BOM first
    bad = []
    text = f"met {TERM} and {NAME}\r\nkey {AKIA}\r\nend\r\n"            # PowerShell writes CRLF
    redacted = f"met {MARK} and {MARK}\r\nkey {MARK}\r\nend\r\n"
    for bom, enc in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
                     (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF32_LE, "utf-32-le"),
                     (codecs.BOM_UTF32_BE, "utf-32-be")):
        rc, out, err = cli(v, bom + text.encode(enc))
        want = bom + redacted.encode(enc)
        if rc != 0 or out != want or "redact: 3 substitution(s)" not in err:
            bad.append((enc, rc, out[:60], want[:60], err))
    c.ok(not bad, "UTF-16 (PowerShell 5.1 `>`), UTF-32 and UTF-8 with a BOM are redacted and written"
         " back in the same encoding with the BOM", repr(bad)[:1500])

    # 4. not UTF-8 and no BOM, or not what the BOM says: exit 2, nothing on stdout
    bad = []
    for name, data in (("cp1251", f"met {TERM} today\n".encode("cp1251")),
                       ("UTF-8 with a stray byte", f"met {TERM} ".encode("utf-8") + b"\xff\n"),
                       ("UTF-16 LE without a BOM", f"met {NAME} today\r\n".encode("utf-16-le")),
                       ("UTF-8 + UTF-16 appended by >>",
                        f"met {NAME}\n".encode("utf-8") + f"and {NAME}\r\n".encode("utf-16")),
                       ("UTF-16 BOM over an odd byte count", codecs.BOM_UTF16_LE + f"{NAME}".encode("utf-16-le") + b"x"),
                       # `echo ... >>` from cmd or Git Bash appends UTF-8 to a UTF-16 file; at an even
                       # byte count the whole decodes as UTF-16 (the tail as CJK) and the term hid in it
                       ("UTF-16 with UTF-8 appended (even length)", (lambda b: b if len(b) % 2 == 0 else b + b"\n")(
                           codecs.BOM_UTF16_LE + "notes\r\n".encode("utf-16-le") + f"met {NAME} today\n".encode("utf-8")))):
        rc, out, err = cli(v, data)
        if rc != 2 or out != b"" or "re-save it as UTF-8" not in err or "substitution" in err:
            bad.append((name, rc, out[:60], err))
    c.ok(not bad, "input that is not UTF-8 and has no BOM, or that its BOM misdescribes, exits 2 and"
         " writes nothing", repr(bad)[:1500])

    return c.done()


if __name__ == "__main__":
    sys.exit(main())
