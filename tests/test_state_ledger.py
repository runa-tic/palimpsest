"""State ledger: append-only facts, deterministic fold, and the three fixes from the 2026-09
audit of the vault it came from — shared last-seen, a git probe that sees unpushed and dirty
work, and a failed probe that records nothing when THIS machine is the one offline.
"""
import json, subprocess, sys
from pathlib import Path
from _util import Checks, make_vault, git, run, tempdir, write


def st(v, *args, env=None):
    return run(v, "state.py", *args, env={"PALIMPSEST_MACHINE": "alpha", **(env or {})})


def main() -> int:
    c = Checks("state ledger")
    v = make_vault()
    write(v, ".gitignore", "__pycache__/\nState/Register.md\nState/current.json\nState/.observed.json\n")
    write(v, "10 Notes/x.md", "x\n")
    git(v, "add", "-A"); git(v, "commit", "-q", "-m", "seed")

    r = st(v, "register", "my-api", "--kind", "service", "--alias", "the api", "--hot")
    c.ok(r.returncode == 0, "register an entity", r.stdout + r.stderr)
    r = st(v, "add", "my-api", "host", "server-1")
    c.ok(r.returncode == 0 and "added" in r.stdout, "add a fact", r.stdout + r.stderr)
    r = st(v, "add", "the api", "host", "server-0", "--since", "2020-01-01")
    c.ok("WARNING" in r.stdout and "does NOT become the current" in r.stdout,
         "a back-dated fact warns that it will not become current", r.stdout)
    r = st(v, "show", "my-api")
    c.ok("host = server-1" in r.stdout and "supersedes" not in r.stdout.split("host = server-1")[0],
         "fold: latest valid_from wins; alias resolves", r.stdout)
    facts = (v / "State" / "facts.jsonl").read_text().splitlines()
    c.ok(len(facts) == 2, "append-only: both facts are kept", str(len(facts)))
    r = __import__("subprocess").run([sys.executable, "-c", "import sys; sys.path.insert(0, 'tools'); import ask; "
                                      "print(ask.state_context('where does the api run now?')[0])"],
                                     cwd=v, capture_output=True, text=True)
    c.ok("my-api.host = server-1" in r.stdout, "ask.py's state hop puts the current fact in the prompt (by alias)",
         r.stdout + r.stderr)
    r = st(v, "show", "--hot", "--opener")
    c.ok("my-api" in r.stdout and "server-1" in r.stdout, "opener block shows hot entities", r.stdout)
    c.ok((v / "State" / "Register.md").exists(), "fold writes the Register")

    r = st(v, "probe", "--only", "git")
    c.ok("sync.alpha.remote = no upstream" in r.stdout, "git probe without upstream says so", r.stdout)
    remote = tempdir("palimpsest-remote-") / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(v, "remote", "add", "origin", str(remote)); git(v, "push", "-q", "-u", "origin", "main")
    write(v, "10 Notes/y.md", "y\n"); git(v, "add", "10 Notes/y.md"); git(v, "commit", "-q", "-m", "local")
    write(v, "tools/wip.py", "pass\n")
    r = st(v, "probe", "--only", "git")
    c.ok("ahead 1 (unpushed)" in r.stdout and "uncommitted code: tools/wip.py" in r.stdout,
         "git probe reports ahead AND uncommitted code (it used to say 'level')", r.stdout)

    kinds = json.loads((v / "State" / "entities.json").read_text())
    kinds["entities"]["sync.alpha"]["stale_after_h"] = 1
    (v / "State" / "entities.json").write_text(json.dumps(kinds))
    fact = {"t": "2020-01-01T00:00:00Z", "valid_from": "2020-01-01T00:00:00Z", "entity": "sync.alpha",
            "attr": "status", "value": "ok", "kind": "observed", "by": "probe:sync", "source": [], "id": "oldobs0001", "note": ""}
    with (v / "State" / "facts.jsonl").open("a") as fh:
        fh.write(json.dumps(fact) + "\n")
    r = st(v, "lint", env={"PALIMPSEST_MACHINE": "beta"})
    c.ok("stale observation: sync.alpha.status" in r.stdout, "fixture: an old observation reads stale", r.stdout)
    write(v, "State/seen/alpha.json", json.dumps({"sync.alpha.status": "2999-01-01T00:00:00Z"}))
    r = st(v, "lint", env={"PALIMPSEST_MACHINE": "beta"})
    c.ok("stale observation: sync.alpha.status" not in r.stdout,
         "another machine's committed seen file clears the false stale flag", r.stdout)

    # TEST-NET-1: nothing answers it. A plain TCP connect "succeeds" behind a TUN-mode proxy, which
    # is exactly the case the verified-TLS control exists for.
    cfg = {"version": 1, "network_control": ["192.0.2.1"],
           "probes": [{"name": "api", "entity": "my-api", "attr": "status", "cmd": ["false"], "timeout": 5}]}
    write(v, "palimpsest.json", json.dumps(cfg))
    before = len((v / "State" / "facts.jsonl").read_text().splitlines())
    r = st(v, "probe", "--only", "api")
    after = len((v / "State" / "facts.jsonl").read_text().splitlines())
    c.ok(after == before and "network is down" in r.stdout,
         "a failed probe records NOTHING when this machine's own network is down", r.stdout)
    obs = json.loads((v / "State" / ".observed.json").read_text())
    c.ok("probe:api" not in obs, "...and leaves the throttle open to retry next run", str(obs))
    cfg["probes"][0]["cmd"] = ["sh", "-c", "echo active"]
    write(v, "palimpsest.json", json.dumps(cfg))
    r = st(v, "probe", "--only", "api")
    c.ok("my-api.status = active" in r.stdout, "a command probe records its first line of output", r.stdout)
    r = st(v, "probe", "--only", "api")
    c.ok("0 new fact(s), 1 unchanged" in r.stdout, "an unchanged observation appends nothing", r.stdout)
    seen = json.loads((v / "State" / "seen" / "alpha.json").read_text())
    c.ok(all(val.endswith(":00:00Z") for val in seen.values()) and "probe:api" not in seen,
         "the committed seen file holds hour-floored entity.attr keys only", str(seen))
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
