"""Commit guards must see renamed files and file NAMES, not just added/modified contents.

Codex review P1: staged_files() used --diff-filter=ACM, so a rename (R) was never scanned —
rename a note and add a credential in the same commit and both guards inspected nothing.
Also: a deny-listed term in a filename (import_claude names files after the conversation)
passed scan_pii, which only read contents.
"""
import sys
from _util import Checks, make_vault, git, run, write

FAKE_KEY = "AKIA" + "QZXW" * 4                      # synthetic, AWS-shaped
TERM = "zzqprivateterm"                              # synthetic deny-listed term
BODY = "".join(f"line {i} of an ordinary note about retrieval\n" for i in range(40))


def main() -> int:
    c = Checks("scanners: renames + paths")
    v = make_vault()
    write(v, "10 Notes/original.md", BODY)
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")

    git(v, "mv", "10 Notes/original.md", "10 Notes/renamed.md")
    write(v, "10 Notes/renamed.md", BODY + f"aws {FAKE_KEY}\n")
    git(v, "add", "-A")
    status = git(v, "diff", "--cached", "--name-status").stdout
    c.ok(status.startswith("R"), "fixture really is a rename", status)
    r = run(v, "scan_secrets.py")
    c.ok(r.returncode == 1 and "AWS access key id" in r.stdout, "scan_secrets blocks a credential in a renamed file",
         r.stdout.strip()[-300:])

    vp = make_vault()
    write(vp, f"10 Notes/creds {FAKE_KEY}.md", "clean body\n")
    git(vp, "add", "-A")
    r = run(vp, "scan_secrets.py")
    c.ok(r.returncode == 1, "scan_secrets blocks a credential in a staged PATH", r.stdout.strip()[-300:])
    c.ok(FAKE_KEY not in r.stdout, "...and does not print it raw", r.stdout)

    v2 = make_vault()
    write(v2, ".gitignore", "tools/.redact_terms.txt\n")     # as in a real vault: the deny list is local
    write(v2, "tools/.redact_terms.txt", TERM + "\n")
    write(v2, f"10 Notes/meeting with {TERM}.md", "nothing sensitive in the body\n")
    git(v2, "add", "-A")
    r = run(v2, "scan_pii.py")
    c.ok(r.returncode == 1, "scan_pii blocks a deny-listed term in a staged PATH", r.stdout.strip()[-300:])
    c.ok(TERM not in r.stdout, "the term itself is never printed", r.stdout)

    v3 = make_vault()
    write(v3, ".gitignore", "tools/.redact_terms.txt\n")
    write(v3, "tools/.redact_terms.txt", TERM + "\n")
    write(v3, "10 Notes/clean.md", BODY)
    git(v3, "add", "-A")
    r = run(v3, "scan_pii.py")
    c.ok(r.returncode == 0, "a clean commit still passes", r.stdout)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
