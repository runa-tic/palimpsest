"""Bytes that do not decode cleanly are checked in every likely reading, not in the one their first bytes name.

Review of 2026-10-02, second pass. The commit guards and the redact CLI each trusted one reading
of a file, the one its first bytes name, and the guards one reading of a staged name; all three
also took a deny list they could not read for no deny list at all.

A file's first bytes say how it began, not what a later writer appended: PowerShell 5.1 `>>`
appends UTF-16 to anything, Git Bash appends UTF-8 to a UTF-16 file, cmd.exe's `echo >>` appends
in the console's code page (cp866 on a Russian Windows), and other programs append in the ANSI one
(cp1251, cp1252). Every value here is synthetic, and every file is built from bytes and staged
straight into the index (git hash-object, update-index), so no check depends on what the
filesystem or a line-ending setting does to it. The byte layouts are built here from the
encodings those programs are understood to use; none of the programs was run.

 1. scan_secrets.readings is the one list of readings: clean UTF-8, with or without a UTF-8 BOM,
    has one, the reading it always had; a UTF-16 BOM, a NUL or other control byte, or a byte that
    is not UTF-8 adds UTF-8 without its NULs, cp1251, cp1252, cp866 and UTF-16 in both byte orders
    from byte 0 and from byte 1. Tab, LF, FF and CR are not such control bytes.
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
 6. A deny-listed name in a staged file NAME that is not UTF-8 (cp1251, cp866) blocks scan_pii,
    and no line prints the name, its bytes as escapes included. A key in such a name still blocks
    scan_secrets, masked (it did before: a key is ASCII), and so does a secret access key that
    only a code page reading of the name shows, masked in the label as well.
 7. The redact CLI exits 2 and writes nothing for a UTF-16 file followed by (D1) a cp1251 line,
    (D2) UTF-16 after an odd number of bytes, (D3) a cp866 line. A file that is UTF-16 throughout
    is still redacted in UTF-16, byte for byte.
 8. A deny list that is there but cannot be read (a directory in its place; mode 000; a dangling
    link) blocks scan_pii with a message that says so, and makes the redact CLI exit 2 with
    nothing written. No deny list at all still passes.
 9. The recorder still records with such a list: hook_record exits 0, the note is written with
    its credentials masked, and stderr says once that deny-listed terms were not redacted. The
    commit guard then blocks that note, while the list is unreadable and after it is readable.

Set PALIMPSEST_TOOLS to a tools/ tree from before these fixes to see the checks fail; 2(b), the
passing halves of 5 and 8, the key half of 6 and the second half of 7 held before and pass there.

Review of 2026-10-04, of the fixes above: what the other readings still let through, and what
they cost or broke.

10. UTF-16 text with no NUL byte (a Cyrillic word with no space or line end: bytes like 21 04 38
    04, valid UTF-8 made of control bytes) is blocked by scan_pii and refused by the redact CLI,
    in either byte order, alone, and after an ASCII line. So is the name in a UTF-32 section, and
    in a UTF-16 file with U+0000 between its letters.
11. A file that starts as a known binary format does has the usual reading alone, under 5MB and
    over it: a file of noise after a PNG signature, and a minimal head of each format listed. A
    Cyrillic name in cp1251 inside such a file is not seen, as before the other readings were
    added; an ASCII key and an ASCII name after the PNG signature still block. A note that
    merely begins with the letters such a format begins with ("BM", "MZ", "ID3", "RIFF", ...)
    keeps every reading, and so does a file with a BOM before the signature.
12. In another reading scan_pii matches BLOCK-tier terms only and prints no WARN line: a UTF-16
    file of Chinese no longer warns about "Ng". scan_secrets lists one value once, whichever
    readings show it and however they spell it, prints no control character, and lists two
    occurrences of one key in an appended part as two. The redact CLI's refusal (7) names the
    reading that shows the term, and a credential in another reading refuses as a term does.

With the tools from before the 2026-10-04 fixes the checks from 10 on fail, and so do the parts
of 1 and 7 added with them, except the checks that say "held before" in their names: those passed
there too, and are here so that they keep passing.
"""
import codecs, hashlib, json, os, re, subprocess, sys
import _util
from _util import Checks, git, run, write

TERM = "Сидорова"                                       # synthetic deny-listed surname, Cyrillic
NAME = "Zorbanek"                                       # synthetic, Latin
MAIL = "zq.private@example.invalid"                     # synthetic address
AKIA = "AKIA" + "QZXW" * 4                              # synthetic, AWS-shaped
AWS_SECRET = "Ab3dEf9hIj" * 4                           # synthetic, 40 characters
MARK = "[redacted]"
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


def cli(v, data: bytes):
    """(exit code, stdout bytes, stderr) of the redact CLI fed `data`, with a cp1251 stdio."""
    r = subprocess.run([sys.executable, str(v / "tools" / "redact.py")], cwd=v, input=data, capture_output=True,
                       env={**os.environ, "PYTHONIOENCODING": "cp1251"})
    return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")


def noise(n: int) -> bytes:
    """n bytes that look random and are the same on every run and every Python."""
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(b"readings" + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


def ask(v, code: str, data: dict | None = None):
    """What a driver run in vault v prints as JSON: `code` sees scan_secrets as s, redact as redact,
    and `data`, the JSON given. None when it fails."""
    driver = (_util.UTF8_STDIO + "import io, json, sys\nsys.path.insert(0, 'tools')\nimport redact, scan_secrets as s\n"
              "data = json.load(sys.stdin)\n" + code)
    r = subprocess.run([sys.executable, "-c", driver], cwd=v, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", input=json.dumps(data or {}))
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


# How a file of each known binary format begins: a minimal head, built here. None is a real file.
BINARY_HEADS = {
    "PNG": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    "JPEG": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
    "GIF": b"GIF89a\x10\x00\x10\x00\x80\x00\x00",
    "WebP": b"RIFF\x24\x00\x00\x00WEBPVP8 ",
    "WAV": b"RIFF\x24\x00\x00\x00WAVEfmt ",
    "BMP": b"BM\x46\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00\x28\x00\x00\x00",
    "ICO": b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00",
    "PDF": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
    "ZIP": b"PK\x03\x04\x14\x00\x00\x00\x08\x00",
    "gzip": b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03",
    "bzip2": b"BZh91AY&SY\x00\x00",
    "xz": b"\xfd7zXZ\x00\x00\x04",
    "7z": b"7z\xbc\xaf\x27\x1c\x00\x04",
    "RAR": b"Rar!\x1a\x07\x00",
    "MP3 with an ID3 tag": b"ID3\x04\x00\x00\x00\x00\x02\x01TIT2",
    "MP4": b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00",
    "MOV": b"\x00\x00\x00\x14ftypqt  ",
    "Ogg": b"OggS\x00\x02\x00\x00\x00\x00",
    "FLAC": b"fLaC\x00\x00\x00\x22\x10\x00",
    "SQLite": b"SQLite format 3\x00\x10\x00",
    "ELF": b"\x7fELF\x02\x01\x01\x00",
    "Mach-O": b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01",
    "Mach-O universal, or a class file": b"\xca\xfe\xba\xbe\x00\x00\x00\x41",
    "PE": b"MZ\x90\x00" + b"\x00" * 56 + (0x80).to_bytes(4, "little") + b"\x00" * 64 + b"PE\x00\x00\x4c\x01",
    "WOFF": b"wOFF\x00\x01\x00\x00",
    "WOFF2": b"wOF2OTTO\x00\x00",
    "TrueType": b"\x00\x01\x00\x00\x00\x0c\x00\x80",
    "OpenType": b"OTTO\x00\x0b\x00\x80",
}
# Notes that begin with the letters one of those formats begins with, and one with a BOM first.
TEXT_HEADS = [b"BMW service notes\n", b"MZ is a region code\n", b"ID3 tags of the album\n",
              b"RIFF is a container format\n", b"RIFF or WAVE, which is it\n", b"GIF89a is the version\n",
              "GIF89a — формат\n".encode("utf-8"), b"OTTO was the name\n",
              b"true or false\n", b"BZh is not a word\n", b"PK, the notes\n", b"%PDF is how one begins\n",
              b"OggS and fLaC files\n", b"wOFF fonts\n", b"Rar! archive notes\n", b"7z archive notes\n",
              b"SQLite format 3 is its header\n", codecs.BOM_UTF8 + b"\x89PNG\r\n\x1a\n in a note\n"]


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
               "UTF-8 BOM and a stray byte": codecs.BOM_UTF8 + b"note\xff\n",
               "clean, with a tab, a form feed and CRLF": b"a\tb\x0c\r\nc\n",
               "a control byte": b"note\x04\n",
               "ESC": b"note \x1b[31mred\x1b[0m\n",
               "DEL": b"note\x7f\n"}
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
            "UTF-8 BOM and a stray byte": ["", *legacy, *utf16],
            "clean, with a tab, a form feed and CRLF": [""],
            "a control byte": ["", *legacy, *utf16], "ESC": ["", *legacy, *utf16], "DEL": ["", *legacy, *utf16]}
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

    # 6. a staged NAME that is not UTF-8 (Linux allows it; index-only, so any filesystem will do)
    for cp in ("cp1251", "cp866"):
        v = vault(f"{TERM}\n".encode("utf-8"))
        stage(v, b"notes/met " + TERM.encode(cp) + b".md", b"nothing here\n")
        r = run(v, "scan_pii.py")
        out = r.stdout + r.stderr
        c.ok(r.returncode == 1 and f"[path]: 1x Си******  (read as {cp})" in out and "Traceback" not in out,
             f"6. scan_pii blocks a deny-listed name in a staged file name written in {cp}",
             f"rc={r.returncode} {out[-400:]}")
        # With the term in the body as well, the file's label is printed on a content line too.
        stage(v, b"notes/met " + TERM.encode(cp) + b".md", f"met {TERM} today\n".encode("utf-8"))
        out = run(v, "scan_pii.py").stdout
        c.ok(TERM not in out and "\\udc" not in out and out.count("notes/met Си******.md") == 2,
             f"6. ...and no line prints the {cp} name: not as text, not as the escapes of its bytes",
             out[-400:])
    v = vault()
    stage(v, b"notes/" + TERM.encode("cp1251") + b" " + AKIA.encode() + b".md", b"nothing here\n")
    r = run(v, "scan_secrets.py")
    c.ok(r.returncode == 1 and r.stdout.count("AWS access key id") == 1 and AKIA not in r.stdout + r.stderr
         and "Traceback" not in r.stdout + r.stderr,
         "6. scan_secrets blocks a key in a staged file name that is not UTF-8, reported once and masked"
         " (held before)", f"rc={r.returncode} {(r.stdout + r.stderr)[-400:]}")
    # A key is ASCII and stayed readable among the surrogates. A secret found by its label did not,
    # when the byte between the two is a no-break space in cp1251 and cp1252 (0xA0): as a surrogate
    # it is not a space, so the name was committed. The value is also what its label must not print.
    v = vault()
    stage(v, b"notes/aws_secret_access_key=\xa0" + AWS_SECRET.encode() + b".md", b"nothing here\n")
    r = run(v, "scan_secrets.py")
    out = r.stdout + r.stderr
    c.ok(r.returncode == 1 and "AWS secret access key" in out and "(read as cp1251)" in out
         and AWS_SECRET[3:-3] not in out and "Traceback" not in out,
         "6. scan_secrets blocks a secret access key in a file name where only a code page reads the"
         " separator as a space, and masks it in the label too", f"rc={r.returncode} {out[-400:]}")

    # 7. the redact CLI: a UTF-16 file with something else after it
    v = vault(f"{TERM}\n".encode("utf-8"))
    for name, data in (
            ("(D1) a cp1251 line", BOM16 + u16("notes\r\n") + f"met {TERM} today\r\n".encode("cp1251")),
            # shifted by one byte and evened out by another: still valid UTF-16, as CJK
            ("(D2) UTF-16 after an odd number of bytes",
             BOM16 + u16("notes\r\n") + b"x" + u16(f"met {TERM} today\r\n") + b"y"),
            ("(D3) a cp866 line", BOM16 + u16("notes\r\n") + f"met {TERM} today\r\n".encode("cp866"))):
        rc, out, err = cli(v, data)
        c.ok(len(data) % 2 == 0 and rc == 2 and out == b"" and "Nothing was written" in err
             and "re-save it as UTF-8" in err and "substitution" not in err and "Traceback" not in err,
             f"7. the redact CLI exits 2 and writes nothing for a UTF-16 file followed by {name}",
             f"rc={rc} out={out[:80]!r} err={err[-300:]}")
        c.ok(re.search(r"read as (cp1251|cp1252|cp866|utf-8|utf-16-[lb]e)", err) is not None
             and "more than one" not in err and TERM not in err,
             "7. ...and says which reading shows the term, not that the file is in two encodings",
             f"err={err[-300:]}")
    rc, out, err = cli(v, BOM16 + u16("notes\r\n") + f"the key is {AKIA}, kept\n".encode("utf-8"))
    c.ok(rc == 2 and out == b"" and "read as utf-8" in err and "Nothing was written" in err and AKIA not in err,
         "7. the redact CLI exits 2 and writes nothing for a UTF-16 file followed by a key in UTF-8: a"
         " credential in another reading refuses, as a term does (held before)",
         f"rc={rc} out={out[:80]!r} err={err[-300:]}")
    bad = []
    text = f"met {TERM} today\r\nkey {AKIA}\r\nend\r\n"
    for bom, enc in ((BOM16, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        rc, out, err = cli(v, bom + text.encode(enc))
        want = bom + f"met {MARK} today\r\nkey {MARK}\r\nend\r\n".encode(enc)
        if rc != 0 or out != want or "redact: 2 substitution(s)" not in err:
            bad.append((enc, rc, out[:80], err))
    c.ok(not bad, "7. a file that is UTF-16 throughout is still redacted in UTF-16, byte for byte (held before)",
         repr(bad)[:800])

    # 8. a deny list that is there but cannot be read
    def unreadable(how: str, make, undo=lambda p: None) -> None:
        v = vault()
        stage(v, b"10 Notes/n.md", f"met {NAME} today\n".encode("utf-8"))
        make(v / "tools" / ".redact_terms.txt")
        try:
            r = run(v, "scan_pii.py")
            rc, out, err = cli(v, f"met {NAME} today\n".encode("utf-8"))
        finally:
            undo(v / "tools" / ".redact_terms.txt")
        c.ok(r.returncode == 1 and "cannot be read" in r.stdout and "Commit blocked" in r.stdout
             and "Traceback" not in r.stdout + r.stderr and NAME not in r.stdout + r.stderr,
             f"8. scan_pii blocks the commit when the deny list is {how}, and says so",
             f"rc={r.returncode} {(r.stdout + r.stderr)[-400:]}")
        c.ok(rc == 2 and out == b"" and "cannot be read" in err and "substitution" not in err
             and "Traceback" not in err,
             f"8. the redact CLI exits 2 and writes nothing when the deny list is {how}",
             f"rc={rc} out={out!r} err={err[-300:]}")

    unreadable("a directory", lambda p: p.mkdir())
    if os.name == "nt" or os.geteuid() == 0:
        c.skip("8. the deny list with mode 000", "Windows has no such mode, and root reads through it")
    else:
        def lock(p):
            p.write_bytes(f"{NAME}\n".encode("utf-8"))
            p.chmod(0)
        unreadable("mode 000", lock, lambda p: p.chmod(0o600))
    if _util.can_symlink():
        unreadable("a link to a file that is gone", lambda p: os.symlink("not-mounted.txt", p))
    else:
        c.skip("8. the deny list as a dangling link", "this process may not create symlinks")
    v = vault()
    stage(v, b"10 Notes/n.md", f"met {NAME} today\n".encode("utf-8"))
    r = run(v, "scan_pii.py")
    rc, out, err = cli(v, f"met {NAME}, key {AKIA}\n".encode("utf-8"))
    c.ok(r.returncode == 0 and rc == 0 and out == f"met {NAME}, key {MARK}\n".encode("utf-8"),
         "8. no deny list at all is still not an error: scan_pii passes and the CLI masks credentials",
         f"rc={r.returncode} {r.stdout[-200:]} | rc={rc} out={out!r} err={err[-200:]}")

    # 9. the recorder with an unreadable deny list: it records, masks credentials, and says what it did not
    v = vault()
    deny = v / "tools" / ".redact_terms.txt"
    deny.mkdir()
    rows = [{"type": "user", "timestamp": "2026-10-02T10:00:00Z",
             "message": {"role": "user", "content": f"notes from the call with {NAME}"}},
            {"type": "assistant", "timestamp": "2026-10-02T10:00:05Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": f"saved; the key was {AKIA}"}]}}]
    tr = write(v, "fixture/projects/-home-me-proj/abcd1234-0000-0000-0000-000000000000.jsonl",
               "\n".join(json.dumps(row) for row in rows) + "\n")
    env = {k: val for k, val in os.environ.items() if k != "CLAUDE_BRAIN_NO_HOOK"}
    r = subprocess.run([sys.executable, str(v / "tools" / "hook_record.py")], cwd=v, env=env, capture_output=True,
                       input=json.dumps({"transcript_path": str(tr)}, ensure_ascii=False).encode("utf-8"))
    err = r.stderr.decode("utf-8", "replace")
    notes = list((v / "40 Resources" / "Claude Conversations").rglob("*(abcd1234).md"))
    text = notes[0].read_text(encoding="utf-8") if len(notes) == 1 else ""
    c.ok(r.returncode == 0 and len(notes) == 1 and MARK in text and AKIA not in text
         and err.count("deny-listed terms were NOT") == 1 and "Traceback" not in err and NAME not in err,
         "9. the recorder still records when the deny list cannot be read: credentials masked, and one"
         " line on stderr says deny-listed terms were not", f"rc={r.returncode} notes={notes} err={err[-400:]}")
    git(v, "add", "--", "40 Resources")
    blocked = run(v, "scan_pii.py")
    deny.rmdir()
    deny.write_bytes(f"{NAME}\n".encode("utf-8"))
    after = run(v, "scan_pii.py")
    c.ok(blocked.returncode == 1 and "cannot be read" in blocked.stdout and after.returncode == 1
         and "Zo******" in after.stdout and NAME not in blocked.stdout + after.stdout,
         "9. ...and the commit guard keeps that note out of git: while the list is unreadable, and for"
         " the term once it is readable",
         f"{blocked.returncode} {blocked.stdout[-200:]}\n{after.returncode} {after.stdout[-300:]}")

    # 10. UTF-16 with no NUL byte in it: valid UTF-8, made of control bytes
    for name, data in (
            ("an ASCII line, then the name in UTF-16 LE", bytes.fromhex("6e6f74650a" "2104380434043e0440043e0432043004")),
            ("an ASCII line, then the name in UTF-16 BE", bytes.fromhex("6e6f74650a" "042104380434043e0440043e04320430")),
            ("the name alone, in UTF-16 LE", u16(TERM)),
            ("two names joined by U+2014, in UTF-16 LE", u16(f"{TERM}\u2014{TERM}"))):
        v = vault(f"{TERM}\n".encode("utf-8"))
        stage(v, b"10 Notes/n.md", data)
        rp = run(v, "scan_pii.py")
        try:
            valid = b"\x00" not in data and TERM not in data.decode("utf-8")
        except UnicodeDecodeError:
            valid = False
        c.ok(valid and rp.returncode == 1 and "x Си******" in rp.stdout and TERM not in rp.stdout,
             f"10. scan_pii blocks {name}: bytes that are valid UTF-8 with no NUL",
             f"valid UTF-8 without NUL: {valid} rc={rp.returncode} {rp.stdout[-300:]}")
        rc, out, err = cli(v, data)
        c.ok(rc == 2 and out == b"" and "Nothing was written" in err and "substitution" not in err,
             f"10. ...and the redact CLI exits 2 and writes nothing for {name}",
             f"rc={rc} out={out[:60]!r} err={err[-300:]}")
    for name, data, through_cli in (
            ("in UTF-32 with no BOM", TERM.encode("utf-32-le"), False),
            ("in UTF-32 after a UTF-8 line", b"note\n" + TERM.encode("utf-32-le"), False),
            ("in UTF-32 after UTF-16 text", BOM16 + u16("notes\r\n") + TERM.encode("utf-32-le"), True),
            ("in a UTF-16 file with U+0000 between its letters",
             BOM16 + u16("notes\r\n" + "\x00".join(TERM) + "\r\n"), True)):
        v = vault(f"{TERM}\n".encode("utf-8"))
        stage(v, b"10 Notes/n.md", data)
        rp = run(v, "scan_pii.py")
        rc, out, err = cli(v, data) if through_cli else (2, b"", "")
        c.ok(rp.returncode == 1 and "1x Си******" in rp.stdout and rc == 2 and out == b"",
             f"10. scan_pii blocks the name {name}" + (", and the redact CLI writes nothing" if through_cli else ""),
             f"rc={rp.returncode} {rp.stdout[-300:]} | cli rc={rc} out={out[:60]!r} err={err[-200:]}")

    # 11. a known binary format has the usual reading alone
    v = vault(f"{TERM}\n{NAME}\n".encode("utf-8"))
    chunk, head, max_bytes = ask(v, "print(json.dumps([s.CHUNK, s.HEAD, s.MAX_BYTES]))") or (1 << 20, 8192, 5_000_000)
    png = BINARY_HEADS["PNG"]
    cp_line = f"met {TERM} today\r\n".encode("cp1251")
    samples = {f"binary: {k}": b + noise(4000) for k, b in BINARY_HEADS.items()}
    samples["binary: PNG, 3MB"] = png + noise(3_000_000)
    samples["binary: PNG, over 5MB"] = png + noise(max_bytes + 300_000)
    samples.update({f"text: {t[:12]!r}": t + cp_line for t in TEXT_HEADS})
    got = ask(v, "out = {}\n"
                 "known = getattr(s, 'known_binary', lambda head: None)\n"
                 "for name, raw in data.items():\n"
                 "    raw = bytes.fromhex(raw)\n"
                 "    blocks = list(s.text_blocks(io.BytesIO(raw).read, len(raw), 'x.dat'))\n"
                 "    out[name] = [known(raw[:s.HEAD]), sorted({b[1] for b in blocks}), [h for h, _ in s.readings(raw)]]\n"
                 "print(json.dumps(out))\n", {k: b.hex() for k, b in samples.items()}) or {}
    wrong = {k: val for k, val in got.items() if k.startswith("binary") and val != [True, [""], [""]]}
    c.ok(len(got) == len(samples) and not wrong,
         f"11. a file that starts as one of {len(BINARY_HEADS)} known binary formats does has the usual reading"
         " alone, read whole and (over 5MB) in runs", f"{len(got)} of {len(samples)} answered; wrong: {wrong!r}"[:900])
    wrong = {k: val for k, val in got.items() if k.startswith("text")
             and (val[0] is not False or "cp1251" not in val[1] or "cp1251" not in val[2])}
    c.ok(len(got) == len(samples) and not wrong,
         f"11. a note that begins with the letters such a format begins with, or has a BOM before its"
         f" signature, keeps every reading ({len(TEXT_HEADS)} of them)", repr(wrong)[:900])
    stage(v, b"40 Resources/shot.png", png + noise(100_000) + b"\n" + cp_line + noise(1000))
    rp = run(v, "scan_pii.py")
    c.ok(rp.returncode == 0 and "pii-scan: clean" in rp.stdout,
         "11. a Cyrillic name in cp1251 inside a PNG is not seen: the one reading such a file had before"
         " the other readings were added", f"rc={rp.returncode} {rp.stdout[-300:]}")
    stage(v, b"40 Resources/shot.png", png + noise(100_000) + f"\nkey {AKIA} met {NAME}\n".encode() + noise(1000))
    rs, rp = run(v, "scan_secrets.py"), run(v, "scan_pii.py")
    c.ok(rs.returncode == 1 and rs.stdout.count("AWS access key id") == 1 and AKIA not in rs.stdout
         and rp.returncode == 1 and "shot.png: 1x Zo******" in rp.stdout,
         "11. an ASCII key and an ASCII name after a PNG signature still block (held before)",
         f"{rs.returncode} {rs.stdout[-300:]}\n{rp.returncode} {rp.stdout[-300:]}")
    v = vault(f"{TERM}\n".encode("utf-8"))
    for i, t in enumerate(TEXT_HEADS):
        stage(v, f"10 Notes/note {i:02d}.md".encode(), t + cp_line)
    rp = run(v, "scan_pii.py")
    c.ok(rp.returncode == 1 and rp.stdout.count(".md: 1x Си******  (read as cp1251)") == len(TEXT_HEADS),
         "11. ...and scan_pii blocks a cp1251 name in each of those notes (held before)",
         f"rc={rp.returncode} {rp.stdout[-600:]}")

    # 12. another reading: BLOCK-tier terms only, one line per value, no control characters
    v = vault("Ng\nИв\n4471\n".encode("utf-8"))
    stage(v, b"40 Resources/zh.txt", BOM16 + u16("一个最好的办法\r\n" * 300))     # the bytes of 个最 spell "*Ng"
    stage(v, b"40 Resources/house.txt", BOM16 + u16("дом 447б\r\n"))           # 447 and б, bytes 31 04
    stage(v, b"10 Notes/said.md", b"Ng said so\n\xff\n")
    rp = run(v, "scan_pii.py")
    c.ok(rp.returncode == 0 and "(read as" not in rp.stdout and rp.stdout.count("pii-scan WARN") == 1
         and "said.md" in rp.stdout,
         "12. scan_pii matches short terms in the usual reading alone: one WARN line, for the note that"
         " holds the term, and none from another reading", f"rc={rp.returncode} {rp.stdout[-500:]}")
    v = vault()
    pw = "pass" + 'word = "пароль-очень-длинный"'           # in two parts: this file must scan clean itself
    stage(v, b"10 Notes/stray.md", f"{pw}\n".encode("utf-8") + b"\xff\n")
    stage(v, b"10 Notes/wide.txt", BOM16 + u16(f"{pw}\r\n"))
    stage(v, b"10 Notes/twice.txt", BOM16 + u16("notes\r\n") + f"key {AKIA}\nnext line\nkey {AKIA}\n".encode("utf-8"))
    rs = run(v, "scan_secrets.py")
    out = rs.stdout
    c.ok(out.count("stray.md:1  ") == 1 and out.count("wide.txt:1  ") == 1 and "Secret-ish assignment" in out
         and not any(ord(ch) < 32 and ch not in "\r\n" for ch in out),
         "12. scan_secrets lists a password in Cyrillic once, in a UTF-8 note with a stray byte and in a"
         " UTF-16 file, and prints no control character", f"rc={rs.returncode} {out[-700:]!r}")
    c.ok(rs.returncode == 1 and out.count("AWS access key id") == 2 and "twice.txt:2  " in out
         and "twice.txt:4  " in out and AKIA not in out,
         "12. ...and lists two occurrences of one key in an appended part as two, each at its line",
         f"rc={rs.returncode} {out[-500:]}")
    v = vault()
    stage(v, b"10 Notes/ctl.md", ("tok" + 'en = "abcdefghijk\x04"\n').encode("utf-8"))
    out = run(v, "scan_secrets.py").stdout
    c.ok(out.count("ctl.md:1  ") == 1 and 'k\\x04"' in out and "\x04" not in out,
         "12. ...and a control character inside a value it lists is printed as its escape, not as the byte",
         repr(out[-300:]))

    return c.done()


if __name__ == "__main__":
    sys.exit(main())
