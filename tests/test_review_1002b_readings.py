"""Bytes that do not decode cleanly are checked in every likely reading, not in the one their first bytes name.

Review of 2026-10-02, second pass. Both commit guards trusted one reading of a staged file: the
one its first bytes name.

A file's first bytes say how it began, not what a later writer appended: PowerShell 5.1 `>>`
appends UTF-16 to anything, Git Bash appends UTF-8 to a UTF-16 file, cmd.exe's `echo >>` appends
in the console's code page (cp866 on a Russian Windows), and other programs append in the ANSI one
(cp1251, cp1252). Every value here is synthetic, and every file is built from bytes and staged
straight into the index (git hash-object, update-index), so no check depends on what the
filesystem or a line-ending setting does to it. The byte layouts are built here from the
encodings those programs are understood to use; none of the programs was run.

 1. scan_secrets.readings is the one list of readings: clean UTF-8, with or without a UTF-8 BOM,
    has one, the reading it always had; a UTF-16 BOM, a NUL or a byte that is not UTF-8 adds
    UTF-8 without its NULs, cp1251, cp1252, cp866 and UTF-16 in both byte orders from byte 0 and
    from byte 1.
 2. scan_secrets blocks a key, and reports it once, in: (a) UTF-8 appended to a UTF-16 file,
    (a') cp866 appended to one, (c) UTF-16 appended to a UTF-8 file with a BOM, (d) UTF-16 after
    an odd number of bytes. In (b), cp1251 appended to a UTF-8 note, it blocked before too (a key
    is ASCII); the check is there so it keeps doing so.
 3. scan_pii blocks a Cyrillic deny-listed name and a deny-listed address in the same five files,
    counts each once however many readings show it, and prints neither.
 4. The same holds over 5MB, where a file is read in runs: UTF-8 appended to a UTF-16 file is
    found by both guards, by scan_secrets at its line, and UTF-16 appended to a UTF-8 note by
    scan_pii.
 5. A clean UTF-8 corpus full of near misses passes both guards, and a clean note whose bytes
    spell a deny-listed term in cp1251 passes too: clean UTF-8 has one reading. The same bytes
    with one stray byte after them are blocked, which is what shows the first passed for that
    reason. Files that are not clean and hold nothing (UTF-16, cp1251, a small binary) pass.

Set PALIMPSEST_TOOLS to a tools/ tree from before these fixes to see the checks fail; 2(b) and the passing halves of 5 held before and pass there.
"""
import codecs, hashlib, json, subprocess, sys
import _util
from _util import Checks, run, write

TERM = "Сидорова"                                       # synthetic deny-listed surname, Cyrillic
MAIL = "zq.private@example.invalid"                     # synthetic address
AKIA = "AKIA" + "QZXW" * 4                              # synthetic, AWS-shaped
BOM16 = codecs.BOM_UTF16_LE
SECRET = f"key {AKIA} mail {MAIL} met {TERM}"            # what each mixed file holds, once


def u16(s: str) -> bytes:
    return s.encode("utf-16-le")


def vault(terms: bytes | None = None):
    v = _util.make_vault()
    write(v, ".gitignore", "tools/.redact_terms.txt\n")      # as in a real vault: the deny list is local
    if terms is not None:
        (v / "tools" / ".redact_terms.txt").write_bytes(terms)
    return v


def stage(v, name: bytes, data: bytes) -> None:
    """Put `data` in the index under the path bytes `name`, with no file on disk."""
    oid = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=v, input=data,
                         capture_output=True, check=True).stdout.strip()
    subprocess.run(["git", "update-index", "--add", "-z", "--index-info"], cwd=v,
                   input=b"100644 " + oid + b"\t" + name + b"\0", check=True)


def noise(n: int) -> bytes:
    """n bytes that look random and are the same on every run and every Python."""
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(b"readings" + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


# The mixed files of checks 2 and 3. Each holds SECRET exactly once, in the appended part.
MIXED = {
    "(a) UTF-8 appended to a UTF-16 file":
        BOM16 + u16("notes\r\n") + f"{SECRET}\n".encode("utf-8"),
    "(a') cp866 appended to a UTF-16 file":
        BOM16 + u16("notes\r\n") + f"{SECRET}\r\n".encode("cp866"),
    "(b) cp1251 appended to a UTF-8 note":
        "заметка\n".encode("utf-8") + f"{SECRET}\r\n".encode("cp1251"),
    "(c) UTF-16 appended to a UTF-8 file with a BOM":
        codecs.BOM_UTF8 + "заметка\r\n".encode("utf-8") + BOM16 + u16(f"{SECRET}\r\n"),
    "(d) UTF-16 after an odd number of bytes":
        BOM16 + u16("notes\r\n") + b"x" + u16(f"{SECRET}\r\n"),
}
HELD_BEFORE = "(b)"         # for scan_secrets only: a key is ASCII, and ASCII survived the one reading


def main() -> int:
    c = Checks("review 2026-10-02 (b): readings")

    # 1. the helper's contract, asked of the module itself
    v = vault()
    samples = {"clean": "заметка and ascii\n".encode("utf-8"),
               "clean, UTF-8 BOM": codecs.BOM_UTF8 + "заметка\n".encode("utf-8"),
               "UTF-16 BOM": BOM16 + u16("note\r\n"),
               "a NUL": b"note\x00\n",
               "a byte that is not UTF-8": "заметка\n".encode("cp1251"),
               "UTF-8 BOM and a NUL": codecs.BOM_UTF8 + b"note\x00\n",
               "UTF-8 BOM and a stray byte": codecs.BOM_UTF8 + b"note\xff\n"}
    driver = (_util.UTF8_STDIO + "import json, sys\nsys.path.insert(0, 'tools')\nimport scan_secrets as s\n"
              "out = {}\n"
              "for name, raw in json.load(sys.stdin).items():\n"
              "    raw = bytes.fromhex(raw)\n"
              "    got = list(s.readings(raw))\n"
              "    out[name] = [[h for h, _ in got], got[0][1] == s.decode(raw)]\n"
              "print(json.dumps(out))\n")
    r = subprocess.run([sys.executable, "-c", driver], cwd=v, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", input=json.dumps({k: b.hex() for k, b in samples.items()}))
    legacy = ["cp1251", "cp1252", "cp866"]
    utf16 = ["utf-16-le", "utf-16-le from byte 1", "utf-16-be", "utf-16-be from byte 1"]
    want = {"clean": [""], "clean, UTF-8 BOM": [""],
            "UTF-16 BOM": ["", "utf-8", *legacy, *utf16],
            "a NUL": ["", *legacy, *utf16],
            "a byte that is not UTF-8": ["", *legacy, *utf16],
            "UTF-8 BOM and a NUL": ["", "utf-8", *legacy, *utf16],
            "UTF-8 BOM and a stray byte": ["", *legacy, *utf16]}
    try:
        got = json.loads(r.stdout)
    except ValueError:
        got = {}
    c.ok(got == {k: [w, True] for k, w in want.items()},
         "1. clean UTF-8 has one reading, the one it always had; anything else adds every likely one",
         f"{got!r}\n{r.stderr[-400:]}")

    # 2, 3. mixed files, staged: each guard blocks on what the appended part holds
    for name, data in MIXED.items():
        v = vault(f"{TERM}\n{MAIL}\n".encode("utf-8"))
        stage(v, b"10 Notes/n.md", data)
        rs, rp = run(v, "scan_secrets.py"), run(v, "scan_pii.py")
        c.ok(rs.returncode == 1 and rs.stdout.count("AWS access key id") == 1 and AKIA not in rs.stdout + rs.stderr,
             f"2. scan_secrets blocks a key in {name}, reported once and masked"
             + (" (held before)" if name.startswith(HELD_BEFORE) else ""),
             f"rc={rs.returncode} {(rs.stdout + rs.stderr)[-400:]}")
        out = rp.stdout + rp.stderr
        # 1x: the address is ASCII, which most readings of the appended part show alike
        c.ok(rp.returncode == 1 and "n.md: 1x Си******" in out and "n.md: 1x zq***" in out and TERM not in out
             and MAIL not in out and "Traceback" not in out,
             f"3. scan_pii blocks a Cyrillic name and an address in {name}, counted once, and prints neither",
             f"rc={rp.returncode} {out[-500:]}")

    # 4. over 5MB the file goes through in runs, and the appended part is still read every way
    n = 90_000
    body = "".join(f"line {i:06d} of a transcript\r\n" for i in range(2 * n))
    big16 = BOM16 + u16(body[:len(body) // 2]) + f"{SECRET}\n".encode("utf-8")   # (a): SECRET is line n + 1
    big8 = body.encode("utf-8") + u16(f"met {TERM} today\r\n")              # UTF-16 appended to UTF-8
    v = vault(f"{TERM}\n".encode("utf-8"))
    stage(v, b"40 Resources/big16.txt", big16)
    rs, rp = run(v, "scan_secrets.py"), run(v, "scan_pii.py")
    c.ok(len(big16) > 5_000_000 and rs.returncode == 1 and rs.stdout.count("AWS access key id") == 1
         and f"big16.txt:{n + 1}  " in rs.stdout and AKIA not in rs.stdout,
         "4. scan_secrets finds a key in UTF-8 appended to a UTF-16 file over 5MB, at its line",
         f"rc={rs.returncode} {(rs.stdout + rs.stderr)[-300:]}")
    c.ok(rp.returncode == 1 and "big16.txt: 1x Си******" in rp.stdout and TERM not in rp.stdout,
         "4. scan_pii finds a Cyrillic name in UTF-8 appended to a UTF-16 file over 5MB",
         f"rc={rp.returncode} {(rp.stdout + rp.stderr)[-300:]}")
    v = vault(f"{TERM}\n".encode("utf-8"))
    stage(v, b"40 Resources/big8.txt", big8)
    rp = run(v, "scan_pii.py")
    c.ok(len(big8) > 5_000_000 and rp.returncode == 1 and "big8.txt: 1x Си******" in rp.stdout
         and TERM not in rp.stdout,
         "4. scan_pii finds a Cyrillic name in UTF-16 appended to a UTF-8 note over 5MB",
         f"rc={rp.returncode} {(rp.stdout + rp.stderr)[-300:]}")

    # 5. clean input is read one way, and input that is not clean invents nothing
    hello = "Привет"
    mojibake = hello.encode("utf-8").decode("cp1251")               # what cp1251 makes of those bytes
    near = (f"met {TERM[:-1]} and {TERM[:-1]}ой, wrote to {MAIL.replace('zq.', 'zq-')}\n"
            f"ids {AKIA[:-1]} AKIA-{AKIA[4:]} ASIATIC sk-short xoxo-gossip\n"
            f"{hello}, 李 said 你好; 0x1234 is not an address and CG-11 is not a key\n")
    v = vault(f"{TERM}\n{MAIL}\n{mojibake}\n".encode("utf-8"))
    stage(v, b"10 Notes/near.md", near.encode("utf-8"))
    stage(v, b"10 Notes/bom.md", codecs.BOM_UTF8 + near.encode("utf-8"))
    stage(v, b"10 Notes/" + "заметка 李.md".encode("utf-8"), f"{hello}\n".encode("utf-8"))
    rs, rp = run(v, "scan_secrets.py"), run(v, "scan_pii.py")
    c.ok(rs.returncode == 0 and "secret-scan: clean" in rs.stdout and rp.returncode == 0
         and "pii-scan: clean" in rp.stdout,
         "5. clean UTF-8 notes full of near misses pass both guards, one of them spelling a deny-listed"
         " term in cp1251", f"{rs.returncode} {rs.stdout[-300:]}\n{rp.returncode} {rp.stdout[-300:]}")
    stage(v, b"10 Notes/stray.md", f"{hello}\n".encode("utf-8") + b"\xff\n")
    rp = run(v, "scan_pii.py")
    c.ok(rp.returncode == 1 and "stray.md: 1x" in rp.stdout and "(read as cp1251)" in rp.stdout
         and rp.stdout.count("10 Notes/") == 1 and mojibake not in rp.stdout,
         "5. ...and the same bytes are blocked once a stray byte follows them: one reading was why it passed",
         f"rc={rp.returncode} {rp.stdout[-400:]}")
    v = vault(f"{TERM}\n{MAIL}\n".encode("utf-8"))
    stage(v, b"40 Resources/ps.txt", BOM16 + u16(near.replace("\n", "\r\n")))
    stage(v, b"40 Resources/ansi.txt", near.replace("李", "Li").replace("你好", "hi").encode("cp1251"))
    stage(v, b"40 Resources/shot.png", b"\x89PNG\r\n\x1a\n" + noise(200_000))
    rs, rp = run(v, "scan_secrets.py"), run(v, "scan_pii.py")
    c.ok(rs.returncode == 0 and "secret-scan: clean" in rs.stdout and rp.returncode == 0
         and "pii-scan: clean" in rp.stdout,
         "5. a UTF-16 file, a cp1251 file and a small binary that hold nothing pass both guards",
         f"{rs.returncode} {rs.stdout[-300:]}\n{rp.returncode} {rp.stdout[-300:]}")

    return c.done()


if __name__ == "__main__":
    sys.exit(main())
