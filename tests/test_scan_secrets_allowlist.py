"""scan_secrets.py must read its allow list and skip its own files, both under tools/.

Same "_tools" leftover as rlm.py (found while fixing it): TOOLS = VAULT / "_tools", so the
documented allow list tools/.secret_scan_allow.txt was never read — a known-public value could
not be allowed — and SKIP_PATHS named _tools/ files, so `--all` flagged the scanner's own
pattern file and the local deny list.
"""
import sys
from _util import Checks, make_vault, git, run, write

PUBLIC = "0x" + "ab12" * 10          # synthetic EVM-address-shaped (WARN tier) value


def main() -> int:
    c = Checks("scan_secrets: allow list + self-skip")
    v = make_vault()
    write(v, "10 Notes/contract.md", f"the public contract is {PUBLIC}\n")
    git(v, "add", "-A")
    r = run(v, "scan_secrets.py", "--strict")
    c.ok(r.returncode == 1, "fixture: the value is flagged without an allow entry", r.stdout)
    write(v, "tools/.secret_scan_allow.txt", PUBLIC + "\n")
    r = run(v, "scan_secrets.py", "--strict")
    c.ok(r.returncode == 0, "an allow-list entry in tools/.secret_scan_allow.txt is honoured", r.stdout)
    r = run(v, "scan_secrets.py", "--all")
    c.ok("tools/scan_secrets.py" not in r.stdout, "--all does not flag the scanner's own patterns", r.stdout[-400:])
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
