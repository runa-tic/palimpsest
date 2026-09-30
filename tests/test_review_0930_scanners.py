"""Regressions for the scanner findings of the 2026-09-30 review (scan_secrets, scan_pii, redact).

One check per finding. Every credential here is synthetic and assembled at runtime, so this file
never carries a credential shape of its own. Redaction runs as `python tools/redact.py < text`,
the scanners as the pre-commit hook runs them, each against a throwaway vault.

 1. A Telegram bot token inside a Bot API URL (/bot<token>/), or ending in '-', is caught.
 2. Current OpenAI keys (sk-proj-/sk-svcacct-/sk-admin-, base64url body) are caught whole.
 3. The AWS secret access key next to its label is caught, not only the AKIA id.
 4. A PGP armored private key is caught, and redaction removes its whole block.
 5. A deny list saved with a BOM, as UTF-16 or as cp1251/cp1252 still redacts every term, and
    credentials are redacted whatever state the deny list is in.
 6. --all and PATH mode read files that are not UTF-8; a named file that cannot be read fails.
 7. A staged UTF-16 file is decoded, so both commit guards see what is in it.
 8. A credential in a staged file's NAME is masked in every line the scanner prints.
 9. Only the scanner's own files are exempt: tools/.secrets* is scanned.
10. Staged files under .obsidian/, _media/, node_modules/ are scanned.
11. A deny-listed name of three words blocks a commit.
12. A deny-listed phone number blocks a commit in any separator spelling, and is redacted so.

Rework after the review of fix/scanners (2026-09-30):
13. A deny list that MIXES encodings (UTF-8 plus an appended cp1252 line, UTF-8 plus an appended
    UTF-16 section with its BOM mid-file, even mid-line) keeps every term; scan_pii blocks while the
    list is not clean UTF-8, names the lines by number only, and never passes a listed term.
14. An OpenAI key right after a literal JSON escape (\\n, \\t) is redacted and blocked.
15. A staged file over 5MB is not read into the hook, and is listed as NOT scanned by both guards.
16. The JSON-escaped AWS secret access key is redacted and blocked, and its label survives.
17. A private key quoted with "> " on every line (a thinking callout) is redacted whole.
18. A phone-shaped term does not match inside a longer run of digits (a Telegram id).

Second review of fix/scanners (2026-09-30):
19. A staged TEXT file over 5MB is still scanned (in chunks) by both guards and blocks the commit,
    with the right line number, UTF-16 included; a staged binary over 5MB alone commits; text over
    TEXT_MAX is not scanned and fails.
20. The staged scan spawns a fixed number of git processes, not one or two per staged file.
21. A UTF-16 deny list with CJK or emoji lines is clean and does not re-read them as bytes (which
    over-redacted every transcript), while UTF-8 appended after a UTF-16 section is still read.
"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path
import _util
from _util import Checks, git, run, write

AKIA = "AKIA" + "QZXW" * 4                               # synthetic, AWS-shaped
B64 = "Ab3dEf9hIj" * 8                                   # synthetic key body


VAULTS = []


def make_vault():
    VAULTS.append(_util.make_vault())      # removed when the checks are done
    return VAULTS[-1]


def redact(v, text: str) -> str:
    r = subprocess.run([sys.executable, str(v / "tools" / "redact.py")], cwd=v, input=text.encode("utf-8"),
                       capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.stdout.decode("utf-8", "replace") + ("\nSTDERR " + r.stderr.decode("utf-8", "replace")
                                                  if r.returncode else "")


def scan_path(v, text: str, name: str = "10 Notes/probe.md"):
    write(v, name, text)
    return run(v, "scan_secrets.py", name)


def pii_vault(terms_bytes: bytes):
    v = make_vault()
    write(v, ".gitignore", "tools/.redact_terms.txt\n")
    (v / "tools" / ".redact_terms.txt").write_bytes(terms_bytes)
    return v


def main() -> int:
    c = Checks("review 2026-09-30: scanners")
    v = make_vault()

    # 1. Telegram: the /bot<token>/ URL form, and a secret that ends in '-'
    tok = "7123456789:" + "AAHq" * 8 + "Zx-"
    url = f"curl https://api.telegram.org/bot{tok}/getMe"
    out = redact(v, f"{url}\nbare {tok}\n")
    r = scan_path(v, url + "\n")
    c.ok(tok not in out and tok[:-1] not in out and r.returncode == 1 and "Telegram bot token" in r.stdout,
         "Telegram token in a Bot API URL and one ending in '-' are redacted and blocked",
         out + r.stdout)

    # 2. OpenAI: '_'/'-' early in the body, service-account and admin keys; redacted whole
    keys = ["sk-proj-Ab3d-f9hIj_" + B64, "sk-svcacct-" + B64 + "_x-y", "sk-admin-" + B64]
    out = redact(v, "\n".join(f"OPENAI_API_KEY={k} end" for k in keys) + "\n")
    leaked = [k for k in keys if k[-12:] in out]
    r = scan_path(v, "\n".join(keys) + "\n")
    slug = scan_path(v, "see risk-admin-panel-configuration-guide and task-proj-overview-of-the-quarter\n")
    c.ok(not leaked and r.stdout.count("OpenAI key") == 3 and slug.returncode == 0,
         "current OpenAI key formats are redacted whole and blocked (kebab-case prose is not)",
         f"leaked={leaked}\n{r.stdout}\n{slug.stdout}")

    # 3. AWS: the secret access key survives redaction of the id
    sak = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYQZXW" + "zzzzzz"       # 40 chars, synthetic
    creds = f"[default]\naws_access_key_id = {AKIA}\naws_secret_access_key = {sak}\n"
    out = redact(v, creds)
    r = scan_path(v, f"aws_secret_access_key = {sak}\n")
    c.ok(len(sak) == 40 and sak not in out and r.returncode == 1,
         "the AWS secret access key is redacted and blocked", out + r.stdout)

    # 4. PGP armored private key
    dashes = "-" * 5
    body = [("Qm9n" + B64)[:64], ("Zm9v" + B64)[:64], "c2hvcnQ"]
    armor = (f"{dashes}BEGIN PGP PRIVATE KEY BLOCK{dashes}\nComment: synthetic\n\n" + "\n".join(body)
             + f"\n=Ab1c\n{dashes}END PGP PRIVATE KEY BLOCK{dashes}\n")
    out = redact(v, "exported:\n" + armor + "after\n")
    r = scan_path(v, armor)
    c.ok(not any(b in out for b in body) and "=Ab1c" not in out and "after" in out and r.returncode == 1,
         "a PGP armored private key is blocked and redacted as a whole block", out + r.stdout)

    # 5. deny-list encodings (BOM, UTF-16, cp1251, cp1252); credentials redacted regardless
    bad = []
    for enc, terms in (("utf-8-sig", ["Zelenskaya", "re:qq\\d{4}"]), ("utf-16", ["Petrovsky"]),
                       ("cp1251", ["Сидорова"]), ("cp1252", ["Müller"])):
        vd = pii_vault("\n".join(terms).encode(enc))
        sample = " ".join(t.replace("re:qq\\d{4}", "qq1234") for t in terms)
        out = redact(vd, f"{sample} and {AKIA}\n")
        if any(t in out for t in sample.split()) or AKIA in out or "STDERR" in out:
            bad.append((enc, out))
        write(vd, "10 Notes/n.md", f"met {terms[0]} today\n")
        git(vd, "add", "10 Notes/n.md")
        p = run(vd, "scan_pii.py")
        if p.returncode != 1 or "Traceback" in p.stderr:
            bad.append((enc, "scan_pii", p.returncode, p.stdout[-200:], p.stderr[-300:]))
    c.ok(not bad, "a deny list saved with a BOM, as UTF-16 or in a legacy codepage still redacts and blocks",
         repr(bad)[:900])

    # 6. --all / PATH: a non-UTF-8 file is scanned, and a named file that cannot be read fails
    v6 = make_vault()
    (v6 / "notes").mkdir()
    (v6 / "notes" / "latin1.txt").write_bytes(b"caf\xe9 " + AKIA.encode() + b"\n")
    ra, rp = run(v6, "scan_secrets.py", "--all"), run(v6, "scan_secrets.py", "notes")
    big = v6 / "big.bin"             # binary over 5MB: not scanned, and named, so a failure
    big.write_bytes(os.urandom(5_100_000))
    rb = run(v6, "scan_secrets.py", "big.bin")
    c.ok(ra.returncode == 1 and rp.returncode == 1 and rb.returncode == 1 and "big.bin" in rb.stdout,
         "--all and PATH scan a non-UTF-8 file; a named file that is not scanned is a failure",
         f"{ra.returncode} {rp.returncode} {rb.returncode}\n{ra.stdout[-200:]}\n{rb.stdout[-200:]}")

    # 7. staged UTF-16 file: both guards read it
    v7 = pii_vault(b"zzqprivateterm\n")
    (v7 / "40 Resources").mkdir()
    (v7 / "40 Resources" / "dump.txt").write_bytes(f"key {AKIA}\r\nzzqprivateterm\r\n".encode("utf-16"))
    (v7 / "40 Resources" / "nobom.txt").write_bytes(f"key {AKIA}\n".encode("utf-16-le"))
    git(v7, "add", "40 Resources")
    rs, rp = run(v7, "scan_secrets.py"), run(v7, "scan_pii.py")
    c.ok(rs.returncode == 1 and rs.stdout.count("AWS access key id") == 2 and rp.returncode == 1,
         "a staged UTF-16 file (with or without BOM) is scanned by both guards", rs.stdout + rp.stdout)

    # 8. credential in the file NAME and body: never printed raw
    v8 = make_vault()
    write(v8, f"10 Notes/creds {AKIA}.md", f"aws {AKIA}\n")
    git(v8, "add", "-A")
    r = run(v8, "scan_secrets.py")
    c.ok(r.returncode == 1 and AKIA not in r.stdout, "a credential in the file name is masked in every finding",
         r.stdout)

    # 9. tools/.secrets* is not exempt by prefix
    v9 = make_vault()
    write(v9, "tools/.secrets.env", f"AWS_KEY={AKIA}\n")
    write(v9, "tools/.secrets", f"AWS_KEY={AKIA}\n")
    git(v9, "add", "-A")
    r = run(v9, "scan_secrets.py")
    c.ok(r.returncode == 1 and "tools/.secrets.env" in r.stdout and "tools/.secrets:" in r.stdout,
         "tools/.secrets and tools/.secrets.env are scanned when staged", r.stdout)

    # 10. staged files in SKIP_DIRS are scanned
    v10 = make_vault()
    oai = "sk-" + "Ab3dEf9hIj" * 4
    write(v10, ".obsidian/plugins/copilot/data.json", f'{{"openAIApiKey": "{oai}"}}\n')
    write(v10, "10 Notes/_media/creds.txt", f"{AKIA}\n")
    write(v10, "node_modules/x/.env", f"{AKIA}\n")
    git(v10, "add", "-A")
    r = run(v10, "scan_secrets.py")
    c.ok(r.returncode == 1 and all(p in r.stdout for p in (".obsidian/plugins", "_media/creds.txt", "node_modules/x")),
         "staged files under .obsidian/, _media/ and node_modules/ are scanned", r.stdout)

    # 11. multi-word names block
    v11 = pii_vault("Mary Ann Lee\nМария Анна Сидорова\n".encode())
    write(v11, "10 Notes/n.md", "lunch with mary ann lee and Мария Анна Сидорова\n")
    git(v11, "add", "-A")
    r = run(v11, "scan_pii.py")
    c.ok(r.returncode == 1 and "Mary" not in r.stdout and "not blocking" not in r.stdout,
         "a deny-listed name of three words blocks the commit", r.stdout)

    # 12. phone numbers block in any separator spelling and are redacted so
    v12 = pii_vault(b"+1 555-0100 77\n")
    write(v12, "10 Notes/n.md", "call me on +1 (555) 0100-77\n")
    git(v12, "add", "-A")
    r = run(v12, "scan_pii.py")
    out = redact(v12, "ring 1-555-010077 or +1 555 0100 77 today\n")
    c.ok(r.returncode == 1 and "0100" not in out and "today" in out,
         "a deny-listed phone number blocks and is redacted in any separator spelling", r.stdout + out)

    # 13. mixed-encoding deny lists: no term is lost, and scan_pii fails closed with line numbers
    bad = []
    mixed = {
        # PowerShell 5.1 Add-Content appends in the ANSI codepage to the UTF-8 file setup writes
        "utf-8 + cp1252 line": ("Сидорова\nPetrovsky\n".encode() + "Müller\n".encode("cp1252"),
                                ["Сидорова", "Petrovsky", "Müller"], True),
        # PowerShell 5 `>>` appends UTF-16 with a BOM; also to a file with no final newline
        "utf-8 + utf-16 section": ("Сидорова\nAlpha\n".encode() + "Petrovsky\r\nИванова\r\n".encode("utf-16"),
                                   ["Сидорова", "Alpha", "Petrovsky", "Иванова"], False),
        "utf-8 (no newline) + utf-16": ("Сидорова\nAlpha".encode() + "Petrovsky\r\n".encode("utf-16"),
                                        ["Сидорова", "Alpha", "Petrovsky"], False),
        "utf-8 + bomless utf-16le": ("Alpha\n".encode() + "Kowalski\r\nИванова\r\n".encode("utf-16-le"),
                                     ["Alpha", "Kowalski", "Иванова"], False),
    }
    for name, (raw, terms, unclean) in mixed.items():
        vd = pii_vault(raw)
        out = redact(vd, " / ".join(f"met {t} today" for t in terms) + "\n")
        missed = [t for t in terms if t in out]
        if missed or "STDERR" in out:
            bad.append((name, "redact", missed, out[-200:]))
        for t in terms:           # every term blocks on its own, in a note written by hand
            write(vd, "10 Notes/n.md", f"met {t} today\n")
            git(vd, "add", "10 Notes/n.md")
            p = run(vd, "scan_pii.py")
            if p.returncode != 1 or "Traceback" in p.stderr or t in p.stdout:
                bad.append((name, "scan_pii", t, p.returncode, p.stdout[-200:], p.stderr[-200:]))
        write(vd, "10 Notes/n.md", "nothing listed here\n")
        git(vd, "add", "10 Notes/n.md")
        p = run(vd, "scan_pii.py")
        want = (1, "not clean UTF-8") if unclean else (0, "clean")
        if p.returncode != want[0] or want[1] not in p.stdout or (unclean and "line 3" not in p.stdout):
            bad.append((name, "scan_pii clean note", p.returncode, p.stdout[-300:], p.stderr[-200:]))
    c.ok(not bad, "a deny list mixing UTF-8 with cp1252 or UTF-16 keeps every term; unclean lists block",
         repr(bad)[:1200])

    # 14. OpenAI key after a literal JSON escape
    keys = ["sk-proj-" + "Ab3dEf9hIj" * 4, "sk-proj-Ab3d-f9hIj_" + B64]
    texts = [f'{{"keys": "a\\n{k}", "t": "b\\t{k}"}}' for k in keys]
    outs = [redact(v, t + "\n") for t in texts]
    r = scan_path(v, "\n".join(texts) + "\n")
    c.ok(all(k[-10:] not in o for k, o in zip(keys, outs)) and r.returncode == 1
         and r.stdout.count("OpenAI key") == 2,
         "an OpenAI key right after a literal \\n or \\t escape is redacted and blocked",
         "\n".join(outs) + r.stdout)

    # 15. an oversize staged file is not read, and is listed as NOT scanned by both guards
    v15 = pii_vault(b"Zelenskaya\n")
    (v15 / "10 Notes" / "_media").mkdir(parents=True)
    (v15 / "10 Notes" / "_media" / "video.mp4").write_bytes(os.urandom(6_000_000))
    write(v15, "10 Notes/n.md", f"aws {AKIA}\n")
    git(v15, "add", "-A")
    rs = run(v15, "scan_secrets.py")
    rp = run(v15, "scan_pii.py")
    c.ok(rs.returncode == 1 and "NOT scanned" in rs.stdout and "video.mp4" in rs.stdout
         and "AWS access key id" in rs.stdout and "video.mp4" in rp.stdout,
         "a staged file over 5MB is listed as NOT scanned (not read), and the rest still blocks",
         f"{rs.stdout}\n{rp.stdout}")

    # 16. AWS secret key in escaped JSON (tool output); the label stays readable
    sak = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYQZXW" + "zzzzzz"
    esc = f'{{\\"SecretAccessKey\\": \\"{sak}\\"}}'
    out = redact(v, f"{esc}\n{{\"SecretAccessKey\": \"{sak}\"}}\n")
    r = scan_path(v, esc + "\n")
    c.ok(sak not in out and out.count("SecretAccessKey") == 2 and r.returncode == 1
         and "AWS secret access key" in r.stdout,
         "a JSON-escaped AWS secret access key is redacted (label kept) and blocked", out + r.stdout)

    # 17. a key quoted in a callout: every line prefixed "> "
    lines_ = [f"{dashes}BEGIN PGP PRIVATE KEY BLOCK{dashes}", "Comment: synthetic", ""] + body + [
        "=Ab1c", f"{dashes}END PGP PRIVATE KEY BLOCK{dashes}"]
    pem = [f"{dashes}BEGIN OPENSSH PRIVATE KEY{dashes}"] + body + [f"{dashes}END OPENSSH PRIVATE KEY{dashes}"]
    outs = [redact(v, "> [!thinking]\n" + "\n".join(("> " + ln).rstrip() for ln in ls) + "\n> after\n")
            for ls in (lines_, pem)]
    c.ok(all(not any(b in o for b in body) and "=Ab1c" not in o and "> after" in o for o in outs),
         "a private key quoted with '> ' on every line is redacted as a whole block", "\n".join(outs))

    # 18. a phone-shaped term is its own number, not any digit run that contains it
    v18 = pii_vault(b"555-0100-77\n")
    write(v18, "10 Notes/n.md", "chat id 9555010077123 and timestamp 15550100771\n")
    git(v18, "add", "-A")
    quiet = run(v18, "scan_pii.py")
    write(v18, "10 Notes/n.md", "call (555) 0100 77\n")
    git(v18, "add", "-A")
    loud = run(v18, "scan_pii.py")
    out = redact(v18, "id 9555010077123 / call 555.0100.77\n")
    c.ok(quiet.returncode == 0 and loud.returncode == 1 and "9555010077123" in out and "0100.77" not in out,
         "a phone-shaped term blocks its own number but not a longer digit run containing it",
         quiet.stdout + loud.stdout + out)

    # 19. large staged TEXT is scanned by both guards and blocks the commit (the size cap skipped it)
    v19 = pii_vault(b"Quintanilla\n")
    git(v19, "config", "core.hooksPath", "tools/githooks")
    filler = "".join(f"filler line {i} of a long session transcript, nothing secret here\n" for i in range(90_000))
    big = "40 Resources/Claude Conversations/big session.md"
    write(v19, big, filler + f"key {AKIA}\nmet Quintanilla\n")
    (v19 / "40 Resources" / "wide.txt").write_bytes((filler + f"key {AKIA}\n").encode("utf-16"))
    git(v19, "add", "-A")
    rs, rp = run(v19, "scan_secrets.py"), run(v19, "scan_pii.py")
    cm = git(v19, "commit", "-qm", "big", check=False)
    size_ok = (v19 / big).stat().st_size > 5_000_000 and (v19 / "40 Resources" / "wide.txt").stat().st_size > 5_000_000
    text_ok = (size_ok and rs.returncode == 1 and f"big session.md:{90_001}" in rs.stdout
               and "wide.txt:90001" in rs.stdout and rp.returncode == 1 and "big session.md: 1x" in rp.stdout
               and cm.returncode != 0)
    git(v19, "rm", "-rq", "--cached", "40 Resources")
    (v19 / "_media").mkdir()
    (v19 / "_media" / "clip.mp4").write_bytes(b"\x00\x00\x00\x20ftypisom" + os.urandom(6_000_000))
    (v19 / "_media" / "raw.bin").write_bytes(os.urandom(6_000_000))
    git(v19, "add", "_media")
    cb = git(v19, "commit", "-qm", "media", check=False)
    huge = v19 / "10 Notes" / "huge.log"
    huge.parent.mkdir(exist_ok=True)
    with open(huge, "wb") as f:
        for _ in range(66):
            f.write(b"y" * 999_999 + b"\n")
    git(v19, "add", "10 Notes/huge.log")
    rh = run(v19, "scan_secrets.py")
    c.ok(text_ok and cb.returncode == 0 and rh.returncode == 1 and "huge.log" in rh.stdout,
         "a staged text file over 5MB is scanned by both guards and blocks; a large binary commits;"
         " text over TEXT_MAX fails", f"{rs.stdout[-600:]}\n{rp.stdout[-300:]}\ncommit={cm.returncode}"
         f" media={cb.returncode} {cb.stderr[-300:]}\n{rh.stdout[-300:]}")

    # 20. a fixed number of git processes for the staged scan, whatever the number of files
    v20 = pii_vault(b"Quintanilla\n")
    for i in range(30):
        write(v20, f"10 Notes/n{i}.md", f"note {i}\n")
    git(v20, "add", "-A")
    shim = Path(tempfile.mkdtemp(prefix="palimpsest-shim-"))
    VAULTS.append(shim)
    (shim / "git").write_text(f'#!/bin/sh\necho "$1" >> "{shim}/log"\nexec "{shutil.which("git")}" "$@"\n')
    (shim / "git").chmod(0o755)
    env = {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    calls = []
    for script in ("scan_secrets.py", "scan_pii.py"):
        (shim / "log").write_text("")
        r = run(v20, script, env=env)
        calls.append((script, r.returncode, (shim / "log").read_text().split()))
    c.ok(all(rc == 0 and 0 < len(log) <= 3 for _, rc, log in calls),
         "the staged scan runs a fixed number of git processes, not one or two per file",
         repr([(s_, rc, len(log), sorted(set(log))) for s_, rc, log in calls]))

    # 21. UTF-16 deny list with CJK / emoji lines: clean, and no byte re-read of them
    v21 = pii_vault("Zorbanek\r\n李\r\n🦊fox\r\n".encode("utf-16"))
    prose = "a long string of things running along"
    out = redact(v21, f"{prose} / met Zorbanek, 李 and 🦊fox\n")
    write(v21, "10 Notes/n.md", f"{prose}\n")
    git(v21, "add", "-A")
    p21 = run(v21, "scan_pii.py")
    v21b = pii_vault("Zorbanek\r\n".encode("utf-16") + "Kowalski\nNowakowski\n".encode())
    out_b = redact(v21b, "met Zorbanek, Kowalski and Nowakowski\n")
    c.ok(prose in out and not any(t in out for t in ("Zorbanek", "李", "🦊fox")) and p21.returncode == 0
         and not any(t in out_b for t in ("Zorbanek", "Kowalski", "Nowakowski")),
         "a UTF-16 deny list with CJK or emoji lines is clean and does not over-redact; appended UTF-8 is read",
         f"{out}\n{p21.returncode} {p21.stdout[-300:]}\n{out_b}")

    for d in VAULTS:
        shutil.rmtree(d, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
