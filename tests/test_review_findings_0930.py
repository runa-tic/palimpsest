"""Regressions for the second external review of the port branch (2026-09-30), one check per finding.

1. Commit guards read type changes: a symlink replaced by a file holding a credential is a `T`
   entry, which the ACMR filter dropped; the guards now read every staged change but deletions.
2. The fold is order-independent when two facts share a second, and flags the tie as a conflict.
3. Every caller of vault_push.py allows its worst case (lock wait + network), so it is never
   killed inside its own rebase-abort / lock-release cleanup.
4. up -> down -> up at one valid_from ends at "up" (the third fact used to collide with the first
   fact's id and be dropped), while a plain retry of the current fact is still a no-op.
5. ask.py's state block carries the same STALE / CONFLICT warnings `state.py show` prints.
"""
import itertools, json, os, subprocess, sys
import _util
from _util import Checks, TOOLS_SRC, git, make_vault, run, write

FAKE_KEY = "AKIA" + "QZXW" * 4                      # synthetic, AWS-shaped


def load(v, name):
    sys.path.insert(0, str(v / "tools"))
    sys.modules.pop(name, None)
    return __import__(name)


def main() -> int:
    c = Checks("review findings 2026-09-30")

    # 1. type change
    v = make_vault()
    write(v, "10 Notes/real.md", "note\n")
    # The symlink is committed straight into the index (mode 120000), which needs no symlink
    # rights: a Windows account without them cannot os.symlink, but its vault can hold links.
    target = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=v, input="real.md",
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.strip()
    # Git for Windows defaults to core.symlinks=false, under which a file replacing a link keeps
    # the link's mode and stages as M (still scanned); the T path needs links to be real.
    git(v, "config", "core.symlinks", "true")
    git(v, "add", "-A")
    git(v, "update-index", "--add", "--cacheinfo", f"120000,{target},10 Notes/link.md")
    git(v, "commit", "-q", "-m", "seed")
    (v / "10 Notes" / "link.md").unlink(missing_ok=True)
    write(v, "10 Notes/link.md", f"key {FAKE_KEY}\n")
    git(v, "add", "-A")
    st_line = git(v, "diff", "--cached", "--name-status").stdout.strip()
    r = run(v, "scan_secrets.py")
    c.ok(st_line.startswith("T") and r.returncode == 1, "scan_secrets blocks a credential in a type-changed file",
         f"{st_line} / rc={r.returncode} / {r.stdout.strip()[-200:]}")

    # 2 + 4. ledger ordering and identity
    v = make_vault()
    st = load(v, "state")
    a = {"t": "2026-09-30T01:00:00Z", "valid_from": "2026-09-30T01:00:00Z", "entity": "api", "attr": "status",
         "value": "up", "kind": "observed", "id": "a"}
    b = dict(a, value="down", id="b")
    values = {st.fold(list(p))["api"]["status"]["value"] for p in itertools.permutations([a, b])}
    c.ok(len(values) == 1, "fold: same-second facts give one answer in any line order", str(values))
    rec = st.fold([a, b])["api"]["status"]
    c.ok(any("same second" in (x.get("reason") or "") for x in rec.get("conflicts", [])),
         "fold: a same-second disagreement is flagged as a conflict", json.dumps(rec.get("conflicts")))
    path = v / "State" / "facts.jsonl"
    st.STATE = path.parent
    vf = "2026-09-30T02:00:00Z"
    for val in ("up", "down", "up"):
        st.append(st.make_fact("api", "status", val, "asserted", "test", [], vf), path)
    facts, _ = st.load_facts(path)
    c.ok(st.fold(facts)["api"]["status"]["value"] == "up" and len({f["id"] for f in facts}) == 3,
         "up -> down -> up at one valid_from ends at up, three distinct ids", str([(f["value"], f["id"]) for f in facts]))
    c.ok(st.append(st.make_fact("api", "status", "up", "asserted", "test", [], vf), path) is False,
         "retrying the current fact is still a no-op")

    # 3. timeout budgets
    vp = load(v, "vault_push")
    cfg = load(v, "config")
    hook_src = (TOOLS_SRC / "hook_session_start.py").read_text()
    t = cfg.DEFAULTS["timeouts"] if hasattr(cfg, "DEFAULTS") else cfg.load()["timeouts"]
    c.ok(t["pull"] > vp.LOCK_WAIT_S + vp.NET_TIMEOUT, "sync pull step outlasts vault_push's worst case",
         f"{t['pull']} vs {vp.LOCK_WAIT_S}+{vp.NET_TIMEOUT}")
    c.ok(t["push"] > vp.LOCK_WAIT_S + 2 * vp.NET_TIMEOUT, "sync push step outlasts lock + pull + push",
         f"{t['push']} vs {vp.LOCK_WAIT_S}+2*{vp.NET_TIMEOUT}")
    c.ok('"VAULT_PUSH_LOCK_WAIT": "15"' in hook_src and '"VAULT_PUSH_NET_TIMEOUT": "35"' in hook_src
         and "timeout=60" in hook_src, "session-start hook passes budgets that fit its 60 s timeout")
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + "import sys; sys.path.insert(0,'tools'); import vault_push as v; "
                        "print(v.LOCK_WAIT_S, v.NET_TIMEOUT)"], cwd=v, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "VAULT_PUSH_LOCK_WAIT": "15", "VAULT_PUSH_NET_TIMEOUT": "35"})
    c.ok(r.stdout.split() == ["15", "35"], "vault_push honours the budget overrides", r.stdout + r.stderr)

    # 5. warnings reach the ask.py prompt
    v = make_vault()
    run(v, "state.py", "register", "my-api", "--kind", "service", env={"PALIMPSEST_MACHINE": "alpha"})
    ents = json.loads((v / "State" / "entities.json").read_text())
    ents["entities"]["my-api"]["stale_after_h"] = 1
    (v / "State" / "entities.json").write_text(json.dumps(ents))
    old = {"t": "2020-01-01T00:00:00Z", "valid_from": "2020-01-01T00:00:00Z", "entity": "my-api", "attr": "status",
           "value": "active", "kind": "observed", "by": "probe:x", "source": [], "id": "old0000001", "note": ""}
    tie = dict(old, value="down", id="old0000002")
    with (v / "State" / "facts.jsonl").open("a") as fh:
        fh.write(json.dumps(old) + "\n" + json.dumps(tie) + "\n")
    r = subprocess.run([sys.executable, "-c", _util.UTF8_STDIO + "import sys; sys.path.insert(0, 'tools'); import ask; "
                        "print(ask.state_context('what is the status of my-api?')[0])"],
                       cwd=v, capture_output=True, text=True, encoding="utf-8", errors="replace")
    c.ok("STALE" in r.stdout and "CONFLICT" in r.stdout, "ask.py's state block carries STALE and CONFLICT",
         r.stdout + r.stderr)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
