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
"""
import os, shutil, subprocess, sys
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
    big = v6 / "big.txt"
    big.write_bytes(b"x" * 5_100_000)
    rb = run(v6, "scan_secrets.py", "big.txt")
    c.ok(ra.returncode == 1 and rp.returncode == 1 and rb.returncode == 1 and "big.txt" in rb.stdout,
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
    for d in VAULTS:
        shutil.rmtree(d, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
