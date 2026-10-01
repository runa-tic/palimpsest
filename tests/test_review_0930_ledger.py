"""Regression checks for the 2026-09-30 review of tools/state.py (the State ledger), one per finding.

1. station release re-checks the lease after its pull, so it never frees a lease another machine
   has since taken; take reports a later take that won the push-time rebase.
2. a --since in the future (a local wall time read as UTC) is refused unless --future, and a probe
   whose observation a later-dated fact outranks says so instead of "(was X)".
3. the git probe says the upstream is gone instead of "level".
4. station take registers the lease entity AFTER its pull, so the first take on a machine that
   lacks State/entities.json does not abort its own pull.
5. append() never glues a fact onto a last line that lacks its newline.
6. non-ASCII machine names stay distinct instead of all becoming "machine".
7. a date-only valid_from renders as that date in a UTC-negative timezone.
8. the git probe records non-ASCII paths readably, not as octal escapes.
9. an entity registered with a leading '@' resolves.
10. the network control check counts a TLS peer whose certificate fails verification as "up".
11. the git probe measures against push_remote, not the branch upstream.
"""
import json, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import _util
from _util import Checks, TOOLS_SRC, make_vault, git, run, write

GITIGNORE = "__pycache__/\nState/Register.md\nState/current.json\nState/.observed.json\n"
TMP: list[Path] = []


def st(v, *args, machine="alpha", env=None):
    return run(v, "state.py", *args, env={"PALIMPSEST_MACHINE": machine, **(env or {})})


def vault() -> Path:
    v = make_vault()
    TMP.append(v)
    write(v, ".gitignore", GITIGNORE)
    write(v, "10 Notes/x.md", "x\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")
    return v


def bare(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"palimpsest-{name}-"))
    TMP.append(d)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(d / "r.git")], check=True)
    return d / "r.git"


def clone(remote: Path, name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"palimpsest-{name}-"))
    TMP.append(d)
    subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)
    git(d, "config", "user.name", name); git(d, "config", "user.email", f"{name}@example.invalid")
    shutil.copytree(TOOLS_SRC, d / "tools", dirs_exist_ok=True, ignore=shutil.ignore_patterns("cache", "__pycache__"))
    write(d, "palimpsest.json", json.dumps({"version": 1, "push_remote": "origin"}))
    return d


def two_machines() -> tuple[Path, Path, Path]:
    remote = bare("remote")
    seed = clone(remote, "seed")
    write(seed, ".gitignore", "tools/\npalimpsest.json\n" + GITIGNORE)
    write(seed, ".gitattributes", "State/facts.jsonl merge=union\n")
    write(seed, "10 Notes/shared.md", "shared\n")
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")
    return remote, clone(remote, "mac"), clone(remote, "box")


def fact_line(entity, attr, value, when: str, fid: str) -> str:
    return json.dumps({"t": when, "valid_from": when, "entity": entity, "attr": attr, "value": value,
                       "kind": "asserted", "by": "test", "source": [], "id": fid, "note": ""})


def check_station(c: Checks):
    # 4 (first take on a machine without the registry) and 1 (release after a forced takeover).
    remote, mac, box = two_machines()          # box is cloned before mac registers the station
    r = st(mac, "station", "take", machine="mac")
    assert r.returncode == 0, r.stdout + r.stderr
    r = st(box, "station", "take", "--force", machine="box")
    c.ok(r.returncode == 0 and "lease taken by box" in r.stdout,
         "4. the first take on a machine without State/entities.json pulls cleanly (registry written after)",
         r.stdout + r.stderr)
    r = st(mac, "station", "release", machine="mac")       # mac never pulled box's takeover
    run(box, "vault_push.py", "--pull-only")
    rb = st(box, "station", "show", machine="box")
    c.ok(r.returncode == 1 and "box" in r.stdout and "station lease: box" in rb.stdout,
         "1. release after another machine's forced take refuses and leaves the lease with that machine",
         r.stdout + "\n--- box after pull:\n" + rb.stdout)

    # take: a later take from the other machine that arrives in the push-time rebase is reported.
    later = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run(box, "vault_push.py", "--pull-only")
    with (box / "State" / "facts.jsonl").open("a") as fh:
        fh.write(fact_line("station", "active", "box", later, "boxlater001") + "\n")
    git(box, "add", "State/facts.jsonl"); git(box, "commit", "-q", "-m", "box take"); git(box, "push", "-q", "origin", "HEAD")
    r = st(mac, "station", "take", "--no-pull", "--force", machine="mac")
    c.ok(r.returncode == 1 and "box" in r.stdout.split("push:")[-1],
         "1. take reports that another machine's later take won after the push-time rebase", r.stdout + r.stderr)


def check_future_since(c: Checks):
    v = vault()
    st(v, "register", "my-api", "--kind", "service")
    st(v, "add", "my-api", "status", "up")
    ahead = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    before = (v / "State" / "facts.jsonl").read_text()
    r = st(v, "add", "my-api", "status", "down", "--since", ahead)
    c.ok(r.returncode != 0 and "future" in r.stdout and (v / "State" / "facts.jsonl").read_text() == before,
         "2. add refuses a --since hours in the future (naive time read as UTC) and appends nothing", r.stdout)
    r = st(v, "add", "my-api", "status", "down", "--since", ahead, "--future")
    c.ok(r.returncode == 0 and "added" in r.stdout, "2. ...--future records it deliberately", r.stdout + r.stderr)
    write(v, "palimpsest.json", json.dumps({"version": 1, "probes": [
        {"name": "api", "entity": "my-api", "attr": "status", "cmd": [sys.executable, "-c", "print('up')"], "network": False}]}))
    r = st(v, "probe", "--only", "api")
    c.ok("(was" not in r.stdout and "outranks" in r.stdout and "observed my-api.status = up" in r.stdout,
         "2. a probe outranked by a later-dated fact says so instead of reporting a change", r.stdout)
    r = st(v, "lint")
    c.ok("future" in r.stdout, "2. lint flags the future-dated fact", r.stdout)


def check_gone_upstream(c: Checks):
    v = vault()
    remote = bare("gone")
    git(v, "remote", "add", "origin", str(remote)); git(v, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "--git-dir", str(remote), "branch", "-m", "main", "trunk"], check=True)
    git(v, "fetch", "-q", "--prune", "origin")
    r = st(v, "probe", "--only", "git")
    c.ok("upstream gone" in r.stdout and "= level" not in r.stdout,
         "3. the git probe says the upstream is gone, not 'level'", r.stdout)


def check_missing_newline(c: Checks):
    v = vault()
    st(v, "register", "my-api", "--kind", "service")
    write(v, "State/facts.jsonl", fact_line("my-api", "host", "server-1", "2026-01-01T00:00:00Z", "hand0000001"))
    r = st(v, "add", "my-api", "status", "up")
    rs = st(v, "show", "my-api")
    rl = st(v, "lint")
    c.ok("host = server-1" in rs.stdout and "status = up" in rs.stdout and "malformed=0" in rl.stdout,
         "5. append after a last line without its newline keeps both facts", rs.stdout + rl.stdout)


def check_machine_names(c: Checks):
    v = vault()
    a = st(v, "station", "show", machine="бокс").stdout
    b = st(v, "station", "show", machine="мак").stdout
    d = st(v, "station", "show", machine="!!!").stdout
    e = st(v, "station", "show", machine="My_Box").stdout
    c.ok("this machine: бокс" in a and "this machine: мак" in b and "this machine: machine)" not in d
         and "this machine: my-box" in e,
         "6. non-ASCII machine names stay distinct; ASCII names sanitise as before", a + b + d + e)


def check_date_only_tz(c: Checks):
    v = vault()
    st(v, "register", "flagx", "--kind", "flag")
    st(v, "add", "flagx", "decision", "go", "--kind", "decided", "--since", "2026-09-08")
    r = st(v, "show", "flagx", env={"TZ": "America/New_York"})
    c.ok("decided 2026-09-08" in r.stdout, "7. a date-only valid_from shows its own date west of UTC", r.stdout)


def check_non_ascii_path(c: Checks):
    v = vault()
    write(v, "tools/заметка.py", "pass\n")
    r = st(v, "probe", "--only", "git")
    c.ok("tools/заметка.py" in r.stdout and "\\320" not in r.stdout,
         "8. the git probe records non-ASCII paths readably", r.stdout)


def check_at_id(c: Checks):
    v = vault()
    st(v, "register", "@example_bot", "--kind", "service", "--hot")
    r = st(v, "add", "@example_bot", "host", "vps")
    rs = st(v, "show", "@example_bot")
    c.ok(r.returncode == 0 and "host = vps" in rs.stdout,
         "9. an entity registered with a leading '@' resolves for add and show", r.stdout + rs.stdout)


TLS_CLIENT = r"""
import socket, ssl, sys, threading
sys.path.insert(0, "tools")
import state
cert, key, mode = sys.argv[1], sys.argv[2], sys.argv[3]
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(4)
port = srv.getsockname()[1]
def serve():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
    while True:
        conn, _ = srv.accept()
        if mode == "close":            # a TUN-mode proxy with nothing upstream: accept, then drop
            conn.close(); continue
        try:
            ctx.wrap_socket(conn, server_side=True).close()
        except Exception:
            conn.close()
threading.Thread(target=serve, daemon=True).start()
real = socket.create_connection
state.socket.create_connection = lambda addr, timeout=None: real(("127.0.0.1", port), timeout=timeout)
print("UP" if state.local_network_up(["localhost"]) else "DOWN")
"""


def check_tls_untrusted(c: Checks):
    if not shutil.which("openssl"):
        c.skip("a TLS peer with an unverifiable certificate counts as network up; a dropped connect does not",
               "needs the openssl CLI to make a throwaway certificate (Git Bash or macOS/Linux have one)")
        return
    v = vault()
    d = Path(tempfile.mkdtemp(prefix="palimpsest-tls-"))
    TMP.append(d)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")], check=True, capture_output=True)
    out = {}
    for mode in ("tls", "close"):
        p = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + TLS_CLIENT, str(d / "c.pem"), str(d / "k.pem"), mode],
                           cwd=v, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        out[mode] = p.stdout.strip() + p.stderr[-300:]
    c.ok(out["tls"].startswith("UP") and out["close"].startswith("DOWN"),
         "10. a TLS peer with an unverifiable certificate counts as network up; a dropped connect does not",
         str(out))


def check_push_remote(c: Checks):
    v = vault()
    harness, mine = bare("harness"), bare("mine")
    git(v, "remote", "add", "origin", str(harness)); git(v, "push", "-q", "-u", "origin", "main")
    write(v, "10 Notes/y.md", "y\n"); git(v, "add", "-A"); git(v, "commit", "-q", "-m", "note")
    git(v, "remote", "add", "vault", str(mine)); git(v, "push", "-q", "vault", "HEAD")    # as vault_push does
    write(v, "palimpsest.json", json.dumps({"version": 1, "push_remote": "vault"}))
    r = st(v, "probe", "--only", "git")
    c.ok("sync.alpha.remote = level" in r.stdout,
         "11. the git probe measures against push_remote, not the harness upstream", r.stdout)
    write(v, "10 Notes/z.md", "z\n"); git(v, "add", "-A"); git(v, "commit", "-q", "-m", "note 2")
    r = st(v, "probe", "--only", "git")
    c.ok("ahead 1 (unpushed)" in r.stdout, "11. ...and still sees real unpushed work there", r.stdout)


def main() -> int:
    c = Checks("review 2026-09-30: state ledger")
    try:
        for fn in (check_station, check_future_since, check_gone_upstream, check_missing_newline,
                   check_machine_names, check_date_only_tz, check_non_ascii_path, check_at_id,
                   check_tls_untrusted, check_push_remote):
            try:
                fn(c)
            except Exception as e:
                c.ok(False, f"{fn.__name__} raised", f"{type(e).__name__}: {e}")
    finally:
        for d in TMP:
            _util.rmtree(d, ignore_errors=True)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
