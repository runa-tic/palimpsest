#!/usr/bin/env python3
r"""State ledger — dated facts, deterministic fold, read-time probes.

What the brain relies on about the world *right now* — where a service runs, whether a timer is
alive, which flag is up, which machine holds the vault — lives here as dated, APPEND-ONLY facts,
never as prose in always-loaded files (CLAUDE.md, memory), which have no maintenance path and rot
silently: in the vault this came from, a months-old paragraph saying a stack was down outranked
two current notes and raised a false alarm. `fold` computes the current view deterministically:
per entity+attribute the fact with the latest `valid_from` wins, older ones are kept as superseded,
nothing is edited or deleted. `probe` records what is observable and appends a fact only when a
value CHANGED. The session opener prints `show --hot --opener`; `ask.py` consults the ledger before
searching notes when a question names a registered entity.

Two clocks on every fact: `t` = when recorded, `valid_from` = when true in the world. Age never
decides truth — `stale_after_h` only flags an *observed* value that has not been re-observed.
Design and the failures behind it: notes/ (the State ledger notes).

Files (State/ at the vault root):
  facts.jsonl          append-only ledger, one JSON object per line  (committed; git merge=union)
  entities.json        registry: kinds, ids, aliases, hot, stale_after_h (committed)
  proposed.jsonl       facts awaiting `accept`                          (committed)
  seen/<machine>.json  hourly last-seen per machine, merged by readers  (committed; one writer each)
  extracted/<kind>-<machine>.json   not the ledger: extract_notes / extract_skills' checkpoint,
                       {conversation: content hash} per kind (notes, skills), so one machine
                       does not re-pay model calls the other already made (committed; one writer each)
  Register.md · current.json · .observed.json   regenerated at every fold (gitignore them)

Usage:
  state.py add <entity> <attr> <value> [--kind decided|asserted] [--since WHEN [--future]] [--source S ...] [--by WHO] [--note N]
  state.py show [<entity-or-alias>] [--history] [--json]
  state.py show --hot [--opener]            # what the session opener prints
  state.py fold                             # regenerate Register.md + current.json
  state.py probe [--only sync,git,<name>] [--force]
  state.py retract <entity> <attr> [--source S] [--note N]
  state.py contradict <idA> <idB> --reason R [--confidence 0.6]
  state.py accept <id> | accept --all       # promote from proposed.jsonl
  state.py register <id> --kind K [--alias A ...] [--hot] [--stale-after H] [--desc D]
  state.py station show | take [--force] [--code] | release [--code]
  state.py lint                             # unknown entities, open conflicts, stale, overdue, duplicates
WHEN accepts 2026-09-01, 2026-09-08T15:01Z, 2026-09-08T23:01+08:00, "2026-09-08 23:01 UTC".
Omit --since for "now": a date-only --since means midnight UTC, which loses the fold to any
same-day timestamped fact (add prints a WARNING when that happens). A time without a zone is UTC
too, and one more than a few minutes ahead is refused unless --future is given.

Probes. Two are built in and need no configuration: `sync` (this machine's last sync run, from
tools/.sync_status.json) and `git` (ahead / behind / diverged against push_remote, else the branch
upstream, as of the last fetch, plus uncommitted code). Anything machine-specific is a COMMAND
probe declared in palimpsest.json, so no host, service or path is baked into this file:

  "probes": [
    {"name": "api", "entity": "my-api", "attr": "status",
     "cmd": ["ssh", "-o", "BatchMode=yes", "my-server", "systemctl is-active my-api"],
     "timeout": 20, "min_interval_h": 1, "network": true, "encoding": "utf-8"}
  ]

The value recorded is the command's first line of output, decoded strictly as the probe's
"encoding" (default "utf-8"; a command that prints in a Windows code page needs it named, e.g.
"cp1251"). A first line that does not decode records "probe error (output not <encoding>)" with its
first bytes in hex in the detail, never a string with U+FFFD in place of each undecodable letter:
under that, two different values of one length compared equal. A non-zero exit records "unreachable
(rc N)" — but only if this machine's own network is up: a TLS handshake with one of the
`network_control` hosts (default github.com and 1.1.1.1) completes, where a peer that answers with a
certificate this machine cannot verify still counts as up (a TUN proxy with nothing upstream cannot
send one). With the control down nothing is
recorded and the throttle stays open: a laptop's DNS outage is not a fact about the server.
"""
from __future__ import annotations
import sys, os, re, json, hashlib, argparse, subprocess, socket, stat, tempfile
from pathlib import Path
from datetime import datetime, timezone

try:
    # UTF-8 whatever the code page, as callers read it, and backslashreplace: under "strict" a
    # surrogate (argv or a path that is not UTF-8) crashed the very error that carried it.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parent
VAULT = TOOLS.parent
STATE = VAULT / "State"
FACTS = STATE / "facts.jsonl"
ENTITIES = STATE / "entities.json"
PROPOSED = STATE / "proposed.jsonl"
REGISTER = STATE / "Register.md"
CURRENT = STATE / "current.json"
OBSERVED = STATE / ".observed.json"
SEEN = STATE / "seen"    # <machine>.json: hourly last-seen per machine, committed (one writer each)
SYNC_STATUS = TOOLS / ".sync_status.json"
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
KINDS = ("decided", "asserted", "observed", "retracted", "contradicts")
LEASE_STALE_H = 4        # a station lease older than this may be taken without --force
FUTURE_SLACK_MIN = 5     # a --since later than now by more than this needs --future (clock skew aside)
DEFAULT_KINDS = {"host": {"stale_after_h": None}, "service": {"stale_after_h": 24},
                 "timer": {"stale_after_h": 26}, "flag": {"stale_after_h": 24},
                 "workflow": {"stale_after_h": None}}

sys.path.insert(0, str(TOOLS))
try:
    import config as cfgmod
    CFG = cfgmod.load()
except Exception:
    CFG = {}


def _machine() -> str:
    """This machine's name in the ledger: palimpsest.json "machine", else $PALIMPSEST_MACHINE,
    else the short hostname. Two machines must not share a name — seen/<machine>.json has one
    writer by construction, and the station lease compares names."""
    name = CFG.get("machine") or os.environ.get("PALIMPSEST_MACHINE") or socket.gethostname().split(".")[0]
    # Unicode letters and digits are kept: an ASCII-only class turned every Cyrillic name ("бокс",
    # "мак") into the same fallback, so two machines shared one lease identity and one seen file.
    # "_" still becomes "-" as before, so an ASCII name keeps the identity it already has.
    clean = re.sub(r"(?:[^\w-]|_)+", "-", name.lower()).strip("-")
    return clean or "machine-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


MACHINE = _machine()


# ----------------------------------------------------------------------------- time helpers
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(s: str) -> str:
    """Normalise a human timestamp to ISO-8601 UTC. Naive datetimes are taken as UTC."""
    s = s.strip().replace(" UTC", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2} \d", s):
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso(dt)


def local(iso_s: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not iso_s:
        return "?"
    try:
        return datetime.fromisoformat(iso_s.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except Exception:
        return iso_s


def hours_since(iso_s: str | None) -> float:
    if not iso_s:
        return 1e9
    try:
        return (utcnow() - datetime.fromisoformat(iso_s.replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:
        return 1e9


# ----------------------------------------------------------------------------- registry / ledger
def load_entities() -> tuple[dict, dict, dict]:
    if not ENTITIES.exists():
        return {}, {}, {}
    # utf-8-sig: a registry saved by Windows Notepad starts with a BOM, which json.loads rejects,
    # and every command crashed on it; facts.jsonl and proposed.jsonl are read the same way.
    reg = json.loads(ENTITIES.read_text(encoding="utf-8-sig"))
    kinds, ents = reg.get("kinds", {}), reg.get("entities", {})
    alias = {}
    for eid, e in ents.items():
        alias[eid.lower().lstrip("@")] = eid   # resolve() strips '@' from input, so an '@id' must be keyed without it
        for a in e.get("aliases", []):
            alias[a.lower().lstrip("@")] = eid
    return kinds, ents, alias


def save_entities(reg: dict) -> None:
    """Write a temp file, then rename it over entities.json. Rewriting in place left a truncated
    registry when the write was cut short (a sync step timeout, a full disk), and every command
    then crashed on it. The temp file sits in tools/logs/ (gitignored, same filesystem), so one
    orphaned by a kill is never committed as ledger content."""
    STATE.mkdir(parents=True, exist_ok=True)
    tmp_dir = TOOLS / "logs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(ENTITIES.stat().st_mode)
    except OSError:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    fd, tmp = tempfile.mkstemp(dir=str(tmp_dir), prefix="entities.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(reg, indent=1, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, ENTITIES)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_entity(eid: str, kind: str, desc: str = "") -> None:
    """Register a built-in probe's entity on first use, so a fresh vault needs no setup for them."""
    reg = json.loads(ENTITIES.read_text(encoding="utf-8-sig")) if ENTITIES.exists() else {}
    reg.setdefault("kinds", dict(DEFAULT_KINDS))
    ents = reg.setdefault("entities", {})
    if eid not in ents:
        ents[eid] = {"kind": kind, "desc": desc}
        save_entities(reg)


def resolve(name: str, alias: dict) -> str | None:
    return alias.get(name.strip().lower().lstrip("@"))


def load_facts(path: Path = FACTS) -> tuple[list[dict], int]:
    facts, bad = [], 0
    if not path.exists():
        return facts, bad
    # utf-8-sig: behind a BOM the first line did not parse and the first fact silently counted as bad.
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            facts.append(json.loads(ln))
        except Exception:
            bad += 1
    return facts, bad


def damaged(bad: int) -> str:
    """What every reader of the fold says when `bad` ledger lines did not parse, or "". Only lint
    reported the count. With the newest fact of an attribute cut short (a kill mid-append, a bad
    merge), the fold falls back to the fact before it, and `show`, the opener, the Register and
    ask.py's prompt gave that older value as the current one with nothing said (review,
    2026-10-06). Which entity the lost line was about is not knowable from a line that does not
    parse, so the warning is about every value."""
    if not bad:
        return ""
    return (f"⚠️ {bad} ledger line{'s' if bad != 1 else ''} in State/facts.jsonl could not be read: a newer "
            "fact may be missing, so any value here may be out of date (`state.py lint`, then repair the file)")


def fact_id(entity: str, attr: str, value, valid_from: str, seq: int = 0) -> str:
    # seq 0 keeps the original id scheme, so every fact already on file keeps its id
    key = f"{entity}|{attr}|{'' if value is None else value}|{valid_from}" + (f"|{seq}" if seq else "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def append(fact: dict, path: Path = FACTS) -> bool:
    """Append one fact. Never rewrites. A no-op only when it would not change anything: the key's
    CURRENT fact already has this value, kind and valid_from (a retry, or a second machine
    recording the same observation). Every append stamps `seq` = how many facts the key already
    has, which orders facts recorded in the same second; and a return to an earlier value
    (up -> down -> up) whose id would collide with the first gets a seq-qualified id instead of
    being dropped as a duplicate (it was, until 2026-09-30, leaving the key at 'down')."""
    STATE.mkdir(parents=True, exist_ok=True)
    existing, _ = load_facts(path)
    same = [f for f in existing if f.get("entity") == fact.get("entity") and f.get("attr") == fact.get("attr")
            and f.get("kind") != "contradicts"]
    if fact.get("kind") != "contradicts" and same:
        cur = fold(same).get(fact["entity"], {}).get(fact["attr"]) or {}
        if (cur.get("value") == fact.get("value") and cur.get("kind") == fact.get("kind")
                and cur.get("valid_from") == fact.get("valid_from")):
            return False
    if fact.get("kind") != "contradicts":
        fact["seq"] = len(same)
    if any(f.get("id") == fact["id"] for f in existing):
        if fact.get("kind") == "contradicts" or not fact.get("seq"):
            return False
        fact["id"] = fact_id(fact["entity"], fact["attr"], fact.get("value"), fact["valid_from"], fact["seq"])
    # A last line without its newline (a hand edit in an editor that adds none, a cut-short write)
    # would absorb this fact into one malformed line, and load_facts drops both.
    lead = ""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            lead = "" if fh.read(1) == b"\n" else "\n"
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(lead + json.dumps(fact, ensure_ascii=False) + "\n")
    return True


def make_fact(entity: str, attr: str, value, kind: str, by: str, source: list[str],
              valid_from: str | None = None, note: str = "") -> dict:
    t = iso(utcnow())
    vf = valid_from or t
    return {"t": t, "valid_from": vf, "entity": entity, "attr": attr, "value": value,
            "kind": kind, "by": by, "source": source, "id": fact_id(entity, attr, value, vf),
            "note": note}


def load_observed(local_only: bool = False) -> dict:
    """Last-seen times. Readers get this machine's file merged (latest wins) with every machine's
    committed State/seen/<machine>.json. .observed.json alone is per-machine, so each machine saw
    the OTHER's unchanged observations as stale forever — in the source vault, a week of a healthy
    second machine read "last seen" days ago. probe passes local_only, so a machine's seen file
    never carries another machine's keys."""
    try:
        obs = json.loads(OBSERVED.read_text(encoding="utf-8"))
    except Exception:
        obs = {}
    if local_only:
        return obs
    for p in sorted(SEEN.glob("*.json")):
        try:
            other = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for k, v in other.items():
            if isinstance(v, str) and v > (obs.get(k) or ""):
                obs[k] = v
    return obs


def save_observed(d: dict):
    STATE.mkdir(parents=True, exist_ok=True)
    OBSERVED.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    # The committed copy holds entity.attr keys only, floored to the hour, so it changes at most
    # hourly (one auto-commit) rather than on every probe. Keep stale_after_h above ~2 h.
    shared = {k: v[:13] + ":00:00Z" for k, v in sorted(d.items())
              if isinstance(v, str) and not k.startswith(("probe:", "detail:"))}
    path = SEEN / f"{MACHINE}.json"
    text = json.dumps(shared, indent=1, ensure_ascii=False) + "\n"
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            SEEN.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------- fold
def fold(facts: list[dict]) -> dict:
    """Current view: entity -> attr -> record. Latest valid_from wins (ties: latest t)."""
    hist: dict[tuple[str, str], list[dict]] = {}
    contras: list[dict] = []
    for f in facts:
        if f.get("kind") == "contradicts":
            contras.append(f)
            continue
        hist.setdefault((f["entity"], f["attr"]), []).append(f)
    cur: dict[str, dict[str, dict]] = {}
    for (e, a), fs in hist.items():
        # A total order, so the fold never depends on line order (the union merge of facts.jsonl
        # interleaves two machines' lines): valid_from, then record time, then seq (same-machine
        # order within one second), then value and id as a last, arbitrary but stable resolution.
        order = lambda x: (x.get("valid_from") or x["t"], x["t"], x.get("seq") or 0, str(x.get("value")), x.get("id") or "")
        fs.sort(key=order)
        latest = fs[-1]
        rec = {k: latest.get(k) for k in ("value", "valid_from", "t", "kind", "by", "source", "id", "note")}
        if latest.get("kind") == "retracted":
            rec["value"] = None
        rec["superseded"] = [x["id"] for x in fs[:-1]]
        tied = [x for x in fs[:-1] if order(x)[:3] == order(latest)[:3] and x.get("value") != latest.get("value")]
        if tied:
            # Nothing orders these (two machines, same second): the value shown is a tie-break,
            # not knowledge, so say so where every reader looks — the conflicts list.
            rec["conflicts"] = [{"ids": [x["id"] for x in tied] + [latest["id"]],
                                 "reason": "facts recorded in the same second disagree; shown value is a tie-break",
                                 "confidence": None, "t": latest["t"]}]
        cur.setdefault(e, {})[a] = rec
    for c in contras:
        e, a = c["entity"], c["attr"]
        rec = cur.setdefault(e, {}).setdefault(a, {"value": None, "kind": "contradicts", "t": c["t"],
                                                   "valid_from": c["t"], "source": [], "id": c.get("id"),
                                                   "by": c.get("by"), "note": "", "superseded": []})
        resolved = any(f["t"] > c["t"] for f in hist.get((e, a), []))
        if not resolved:
            rec.setdefault("conflicts", []).append({"ids": c.get("ids"), "reason": c.get("reason"),
                                                    "confidence": c.get("confidence"), "t": c["t"]})
    return cur


def stale_after(eid: str, kinds: dict, ents: dict):
    e = ents.get(eid, {})
    if "stale_after_h" in e:
        return e["stale_after_h"]
    return kinds.get(e.get("kind", ""), {}).get("stale_after_h")


def last_seen(rec: dict, eid: str, attr: str, obs: dict) -> str | None:
    # Recording an observed fact is itself a sighting; a seen time older than it is superseded.
    return max(filter(None, (obs.get(f"{eid}.{attr}"), rec.get("t"))), default=None)


_ISO_DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_BY_DAY = re.compile(r"(?i)\bby\s+(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?(\d{4}-\d{2}-\d{2})\b")


def overdue(cur: dict, today: str | None = None) -> list[tuple[str, str, str, dict]]:
    """(entity, attr, date, record) for each current intention whose date has passed: a `next`
    whose value says "by <date>", and a `deadline` holding a date. Nothing else is read: values
    are prose full of dates. The ledger only changes when someone appends to it, so a `next` that
    was done and recorded in a note alone stays "to do" here for ever; on 2026-10-04 an email
    sent that evening was still "email ... by 2026-10-02" in the ledger, and a session answering
    from it called it unsent. This does not fix the fact; it says the fact needs a look."""
    today = today or datetime.now().date().isoformat()
    out = []
    for eid, recs in sorted(cur.items()):
        for attr, rec in sorted(recs.items()):
            if rec.get("kind") == "retracted" or rec.get("value") is None:
                continue
            value = str(rec.get("value"))
            if attr == "next" or attr.endswith(("_next", ".next")):
                days = _BY_DAY.findall(value)
            elif attr == "deadline" or attr.endswith(("_deadline", ".deadline")):
                days = _ISO_DAY.findall(value)
            else:
                continue
            past = sorted(d for d in days if d < today)
            if past:
                out.append((eid, attr, past[0], rec))
    return out


def is_stale(rec: dict, eid: str, attr: str, kinds: dict, ents: dict, obs: dict) -> bool:
    if rec.get("kind") != "observed":
        return False
    h = stale_after(eid, kinds, ents)
    return bool(h) and hours_since(last_seen(rec, eid, attr, obs)) > h


def src_str(rec: dict) -> str:
    s = rec.get("source") or []
    return ", ".join(s) if isinstance(s, list) else str(s)


def render_value(rec: dict) -> str:
    v = rec.get("value")
    return "∅ (retracted)" if v is None else str(v)


def day(iso_s: str | None) -> str:
    # A date-only --since is stored as midnight UTC; converted to local time it showed the day
    # before anywhere west of UTC. Midnight UTC renders as the date it was given.
    if iso_s and iso_s.endswith("T00:00:00Z"):
        return iso_s[:10]
    return local(iso_s, "%Y-%m-%d")


def when_str(rec: dict, eid: str, attr: str, obs: dict) -> str:
    k = rec.get("kind")
    if k == "observed":
        return f"seen {local(last_seen(rec, eid, attr, obs), '%m-%d %H:%M')}"
    if k == "decided":
        return f"decided {day(rec.get('valid_from'))}"
    if k == "retracted":
        return f"retracted {day(rec.get('valid_from'))}"
    return f"since {day(rec.get('valid_from'))}"


ATTR_ORDER = ["host", "role", "decision", "status", "active", "gate", "ref", "enabled", "armed",
              "present", "scheduler", "remote", "url", "location", "path"]


def attr_order(attr: str):
    return (ATTR_ORDER.index(attr) if attr in ATTR_ORDER else len(ATTR_ORDER), attr)


def write_outputs(cur: dict, facts: list[dict], kinds: dict, ents: dict, bad: int = 0) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    obs = load_observed()
    now = utcnow()
    CURRENT.write_text(json.dumps({"generated": iso(now), "machine": MACHINE, "facts": len(facts),
                                   "current": cur}, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = ["---", "type: dashboard", "tags:", "  - state", "---",
             "# 🧾 State Register",
             f"*Generated {local(iso(now))} local ({iso(now)}) on `{MACHINE}` by `tools/state.py fold` "
             f"from `State/facts.jsonl` ({len(facts)} facts, {len(cur)} entities with facts). "
             "This file is DERIVED — change state with `python tools/state.py add …`, never by editing "
             "this file. Two clocks: *since* = when true in the world, *seen* = last observation.*", ""]
    if bad:
        lines += [f"> {damaged(bad)}", ""]
    hot = [e for e, d in ents.items() if d.get("hot")]
    lines += ["## 🔥 Hot", "", "| entity | attribute | value | when | kind | source |", "|---|---|---|---|---|---|"]
    for eid in hot:
        for attr, rec in sorted(cur.get(eid, {}).items(), key=lambda kv: attr_order(kv[0])):
            flag = " ⚠️ stale" if is_stale(rec, eid, attr, kinds, ents, obs) else ""
            conf = " ⚠️ conflict" if rec.get("conflicts") else ""
            lines.append(f"| **{eid}** | {attr} | {render_value(rec)}{flag}{conf} | {when_str(rec, eid, attr, obs)} "
                         f"| {rec.get('kind')} | {src_str(rec)} |")
    lines.append("")
    by_kind: dict[str, list[str]] = {}
    for eid, d in ents.items():
        by_kind.setdefault(d.get("kind", "other"), []).append(eid)
    lines.append("## 📚 All entities")
    for kind in sorted(by_kind):
        lines += ["", f"### {kind}"]
        for eid in sorted(by_kind[kind]):
            d = ents[eid]
            al = ", ".join(d.get("aliases", [])[:4])
            lines.append(f"- **{eid}**" + (f" *({al})*" if al else "") + (f" — {d['desc']}" if d.get("desc") else ""))
            recs = cur.get(eid, {})
            if not recs:
                lines.append("  - *(no facts yet)*")
            for attr, rec in sorted(recs.items(), key=lambda kv: attr_order(kv[0])):
                flag = " ⚠️ stale" if is_stale(rec, eid, attr, kinds, ents, obs) else ""
                conf = " ⚠️ conflict" if rec.get("conflicts") else ""
                sup = f" · supersedes {len(rec['superseded'])}" if rec.get("superseded") else ""
                lines.append(f"  - `{attr}` = {render_value(rec)}{flag}{conf} — {when_str(rec, eid, attr, obs)}, "
                             f"{rec.get('kind')} by {rec.get('by')}{sup}" + (f" — {src_str(rec)}" if src_str(rec) else "")
                             + (f" — *{rec['note']}*" if rec.get("note") else ""))
    unknown = sorted({f["entity"] for f in facts if f.get("entity") not in ents})
    if unknown:
        lines += ["", "## ❓ Facts on unregistered entities", ""] + [f"- `{u}`" for u in unknown]
    conflicts = [(e, a, c) for e, recs in cur.items() for a, r in recs.items() for c in r.get("conflicts", [])]
    lines += ["", "## ⚠️ Open contradictions", ""]
    lines += ([f"- **{e}.{a}** — {c.get('reason')} (confidence {c.get('confidence')}, ids {c.get('ids')}, "
               f"raised {local(c.get('t'))}) — resolve with `state.py add`" for e, a, c in conflicts] or ["✅ none"])
    stale = [(e, a) for e, recs in cur.items() for a, r in recs.items() if is_stale(r, e, a, kinds, ents, obs)]
    lines += ["", "## ⏳ Stale observations", ""]
    lines += ([f"- **{e}.{a}** — last seen {local(last_seen(cur[e][a], e, a, obs))}" for e, a in stale] or ["✅ none"])
    late = overdue(cur)
    lines += ["", "## ⏰ Overdue", ""]
    lines += ([f"- **{e}.{a}** — names {day}, which has passed: if it is done, append what happened "
               f"(`state.py add {e} {a} \"...\"`)" for e, a, day, _ in late] or ["✅ none"])
    probes = {k: v for k, v in obs.items() if k.startswith("probe:")}
    if probes:
        lines += ["", "## 🔎 Probe runs (this machine)", ""] + [f"- `{k[6:]}` — {local(v)}" for k, v in sorted(probes.items())]
    lines.append("")
    REGISTER.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def refold() -> dict:
    facts, bad = load_facts()
    kinds, ents, _ = load_entities()
    cur = fold(facts)
    write_outputs(cur, facts, kinds, ents, bad)
    return cur


# ----------------------------------------------------------------------------- show
def opener_block(cur: dict, kinds: dict, ents: dict, bad: int = 0) -> str:
    obs = load_observed()
    late = {(e, a) for e, a, _, _ in overdue(cur)}
    out = [f"**State** (fold {local(iso(utcnow()), '%H:%M')} local · [[State Register]])"]
    if bad:
        out.append(f"- {damaged(bad)}")
    for eid, d in ents.items():
        if not d.get("hot"):
            continue
        recs = cur.get(eid, {})
        if not recs:
            out.append(f"- **{eid}** — no facts yet")
            continue
        parts = []
        wanted = d.get("opener")  # registry may pin which attributes the opener shows, in order
        items = [(a, recs[a]) for a in wanted if a in recs] if wanted else sorted(recs.items(), key=lambda kv: attr_order(kv[0]))
        for attr, rec in items:
            flag = " ⚠️ stale" if is_stale(rec, eid, attr, kinds, ents, obs) else ""
            conf = " ⚠️ conflict" if rec.get("conflicts") else ""
            flag += " ⏰ overdue" if (eid, attr) in late else ""
            parts.append(f"{attr}: {render_value(rec)}{flag}{conf} ({when_str(rec, eid, attr, obs)})")
        line = f"- **{eid}** — " + " · ".join(parts)
        if eid == "station":
            holder = (recs.get("active") or {}).get("value")
            if holder == MACHINE:
                line += "  ← this machine holds the lease"
            elif holder in (None, "free"):
                line += "  ← free: `python tools/state.py station take` claims it"
            else:
                line += f"  ⚠️ held by {holder} — `python tools/state.py station take` before hand-editing here"
        out.append(line)
    return "\n".join(out) if len(out) > 1 else ""


def cmd_show(args):
    facts, bad = load_facts()
    kinds, ents, alias = load_entities()
    cur = fold(facts)
    if args.hot and args.opener:
        print(opener_block(cur, kinds, ents, bad))
        return
    if bad and not args.json:
        print(damaged(bad) + "\n")
    if args.hot:
        for eid, d in ents.items():
            if d.get("hot"):
                show_entity(eid, cur, facts, ents, kinds, args.history, args.json, bad)
        return
    if not args.entity:
        print(f"{len(facts)} facts, {len(cur)} entities with facts, {len(ents)} registered. "
              "Give an entity or alias, or --hot.")
        for eid in sorted(cur):
            print(f"  {eid}: " + ", ".join(f"{a}={render_value(r)}" for a, r in sorted(cur[eid].items())))
        return
    eid = resolve(args.entity, alias)
    if not eid:
        print(f"unknown entity {args.entity!r}. Known ids: {', '.join(sorted(ents)) or '(none registered)'}")
        sys.exit(2)
    show_entity(eid, cur, facts, ents, kinds, args.history, args.json, bad)


def show_entity(eid: str, cur: dict, facts: list[dict], ents: dict, kinds: dict, history: bool, as_json: bool,
                bad: int = 0):
    obs = load_observed()
    recs = cur.get(eid, {})
    if as_json:
        # "malformed": ledger lines that did not parse; above 0, "current" may be out of date.
        print(json.dumps({"entity": eid, "registry": ents.get(eid), "current": recs,
                          "history": [f for f in facts if f.get("entity") == eid] if history else None,
                          "malformed": bad},
                         ensure_ascii=False, indent=1))
        return
    d = ents.get(eid, {})
    print(f"{eid}  [{d.get('kind', '?')}]  " + (", ".join(d.get("aliases", [])) if d.get("aliases") else "")
          + (f"\n  {d['desc']}" if d.get("desc") else ""))
    if not recs:
        print("  (no facts yet)")
    for attr, rec in sorted(recs.items(), key=lambda kv: attr_order(kv[0])):
        flag = "  ⚠️ STALE" if is_stale(rec, eid, attr, kinds, ents, obs) else ""
        conf = "  ⚠️ CONFLICT" if rec.get("conflicts") else ""
        print(f"  {attr} = {render_value(rec)}{flag}{conf}")
        print(f"      {rec.get('kind')} · {when_str(rec, eid, attr, obs)} · recorded {local(rec.get('t'))} · by {rec.get('by')}"
              f" · id {rec.get('id')}" + (f" · supersedes {len(rec['superseded'])}" if rec.get("superseded") else ""))
        if src_str(rec):
            print(f"      source: {src_str(rec)}")
        if rec.get("note"):
            print(f"      note: {rec['note']}")
        for c in rec.get("conflicts", []):
            print(f"      conflict: {c.get('reason')} (confidence {c.get('confidence')}, ids {c.get('ids')})")
    if history:
        print("  history:")
        for f in sorted((f for f in facts if f.get("entity") == eid), key=lambda x: (x.get("valid_from") or x["t"], x["t"])):
            print(f"    {f.get('valid_from')}  {f['attr']} = {f.get('value')}  [{f.get('kind')} by {f.get('by')}, recorded {f['t']}, id {f.get('id')}]")


# ----------------------------------------------------------------------------- add / retract / contradict / accept / register
def default_by() -> str:
    sid = os.environ.get("CLAUDE_SESSION_ID")
    return f"session:{sid[:8]}" if sid else f"manual@{MACHINE}"


def since_arg(args) -> str | None:
    """--since as ISO UTC, refused when it lies in the future without --future. A naive time is read
    as UTC, so a local wall time east of UTC landed hours ahead; the fold's latest valid_from then
    outranked every probe observation until that hour came, while probe printed each as a change."""
    if not args.since:
        return None
    vf = parse_when(args.since)
    ahead_min = -hours_since(vf) * 60
    if ahead_min > FUTURE_SLACK_MIN and not args.future:
        print(f"refused: --since {args.since!r} is {vf} UTC, {ahead_min / 60:.1f} h in the future; until then it "
              f"outranks every newer fact and observation on the key. A time without a zone is read as UTC: "
              f"give the offset (…T15:00+03:00), omit --since for now, or pass --future if it is meant.")
        sys.exit(2)
    return vf


def cmd_add(args):
    _, ents, alias = load_entities()
    eid = resolve(args.entity, alias)
    if not eid:
        print(f"unknown entity {args.entity!r} — register it first: state.py register <id> --kind <kind> --alias …")
        sys.exit(2)
    vf = since_arg(args)
    f = make_fact(eid, args.attr, args.value, args.kind, args.by or default_by(), args.source or [], vf, args.note or "")
    # The fold keeps the latest valid_from, so a fact dated earlier than one already on file is
    # recorded as history, not as the current value. A date-only --since (midnight UTC) does this
    # by accident, and it happened twice in the source vault before this warning existed.
    existing, _ = load_facts()
    later = sorted((x for x in existing if x.get("entity") == eid and x.get("attr") == args.attr
                    and x.get("kind") != "contradicts" and (x.get("valid_from") or x["t"]) > f["valid_from"]),
                   key=lambda x: x.get("valid_from") or x["t"])
    if append(f):
        print(f"added {f['id']}  {eid}.{args.attr} = {args.value}  ({args.kind}, since {f['valid_from']})")
        if later:
            w = later[-1]
            print(f"WARNING: this fact does NOT become the current {eid}.{args.attr}: {w['id']} is valid from "
                  f"{w.get('valid_from') or w['t']}, later than yours ({f['valid_from']}). If yours is the current "
                  f"state, re-add it with a full --since timestamp (YYYY-MM-DDTHH:MM:SSZ) or no --since at all.")
    else:
        print(f"no-op: identical fact already in the ledger ({f['id']})")
    refold()


def cmd_retract(args):
    _, ents, alias = load_entities()
    eid = resolve(args.entity, alias)
    if not eid:
        print(f"unknown entity {args.entity!r}")
        sys.exit(2)
    f = make_fact(eid, args.attr, None, "retracted", args.by or default_by(), args.source or [],
                  since_arg(args), args.note or "")
    print(("retracted " if append(f) else "no-op: already retracted ") + f"{eid}.{args.attr} ({f['id']})")
    refold()


def cmd_contradict(args):
    facts, _ = load_facts()
    by_id = {f.get("id"): f for f in facts}
    a, b = by_id.get(args.id_a), by_id.get(args.id_b)
    if not a or not b:
        print("both ids must exist in the ledger")
        sys.exit(2)
    if (a["entity"], a["attr"]) != (b["entity"], b["attr"]):
        print("a contradiction must be between two facts on the same entity and attribute")
        sys.exit(2)
    t = iso(utcnow())
    c = {"t": t, "valid_from": t, "entity": a["entity"], "attr": a["attr"], "value": None,
         "kind": "contradicts", "by": args.by or default_by(), "source": [], "ids": [args.id_a, args.id_b],
         "reason": args.reason, "confidence": args.confidence,
         "id": hashlib.sha1(f"contradicts|{args.id_a}|{args.id_b}|{t}".encode()).hexdigest()[:12], "note": ""}
    append(c)
    print(f"recorded contradiction {c['id']} on {a['entity']}.{a['attr']} — resolve with `state.py add`")
    refold()


def cmd_accept(args):
    props, _ = load_facts(PROPOSED)
    if not props:
        print("nothing proposed")
        return
    keep, n = [], 0
    for p in props:
        if args.all or p.get("id") == args.id:
            n += int(append(p))
        else:
            keep.append(p)
    PROPOSED.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in keep), encoding="utf-8", newline="\n")
    print(f"accepted {n}, {len(keep)} still proposed")
    refold()


def cmd_register(args):
    reg = json.loads(ENTITIES.read_text(encoding="utf-8-sig")) if ENTITIES.exists() else {}
    reg.setdefault("kinds", dict(DEFAULT_KINDS))
    reg.setdefault("entities", {})
    e = reg["entities"].get(args.id, {})
    if args.kind:
        e["kind"] = args.kind
    if "kind" not in e:
        print("--kind is required for a new entity")
        sys.exit(2)
    if args.alias:
        e["aliases"] = sorted(set(e.get("aliases", [])) | set(args.alias))
    if args.hot:
        e["hot"] = True
    if args.stale_after is not None:
        e["stale_after_h"] = args.stale_after
    if args.desc:
        e["desc"] = args.desc
    reg["entities"][args.id] = e
    save_entities(reg)
    print(f"registered {args.id}: {e}")
    refold()


# ----------------------------------------------------------------------------- probes (read-only)
def local_network_up(hosts: list[str] | None = None) -> bool:
    """Control check: can this machine complete a VERIFIED TLS handshake with anything other than
    the target? A bare TCP connect is not enough: behind a TUN-mode proxy or VPN every connect
    succeeds locally even when nothing upstream answers, so the check passed while the machine
    was offline. A handshake that verifies the peer's certificate only completes end to end."""
    import ssl
    ctx = ssl.create_default_context()
    hosts = CFG.get("network_control", ["github.com", "1.1.1.1"]) if hosts is None else hosts
    for host in hosts:
        try:
            with socket.create_connection((host, 443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=host):
                    return True
        except ssl.SSLCertVerificationError:
            # A peer answered with a certificate chain, which a local TUN proxy with nothing
            # upstream cannot do; only this machine cannot verify it (a python.org build with
            # no CA store, a TLS-inspecting proxy). Reading that as "network down" meant no
            # failed probe was ever recorded on such a machine.
            return True
        except (OSError, ssl.SSLError):
            continue
    return False


def probe_sync() -> tuple[list[tuple[str, str, str]], dict]:
    eid = f"sync.{MACHINE}"
    ensure_entity(eid, "timer", f"the sync pipeline on {MACHINE}")
    if not SYNC_STATUS.exists():
        return [(eid, "status", "no run recorded")], {}
    try:
        r = json.loads(SYNC_STATUS.read_text(encoding="utf-8"))
        fin = datetime.fromisoformat(r["finished"])
        age_h = (datetime.now() - fin).total_seconds() / 3600
        stale_h = 26 if (CFG.get("sync", {}) or {}).get("cadence", "daily") == "daily" else 3
        status = "failed: " + ", ".join(r.get("failures", [])) if r.get("failures") else ("stale" if age_h > stale_h else "ok")
        return [(eid, "status", status)], {"last_run": r["finished"], "age_h": round(age_h, 2)}
    except Exception as e:
        return [(eid, "status", f"unreadable receipt ({type(e).__name__})")], {}


def probe_git() -> tuple[list[tuple[str, str, str]], dict]:
    """Where this checkout stands against push_remote (else the branch upstream) as of the last
    fetch (the sync's pull step fetches first), plus code the auto-commit never takes
    (vault_push.CODE). Looking only for "behind" made an unpushed or dirty tree read "level": in
    the source vault a ledger fix sat uncommitted for three days behind a green ledger."""
    eid = f"sync.{MACHINE}"
    ensure_entity(eid, "timer", f"the sync pipeline on {MACHINE}")
    try:
        from vault_push import CODE
    except Exception:
        CODE = ["tools", ".claude", "CLAUDE.md", ".gitignore", ".gitattributes"]
    try:
        # quotePath off: the default wrote a non-ASCII path into the shared ledger as octal escapes.
        p = subprocess.run(["git", "-c", "core.quotePath=false", "status", "-sb", "--porcelain", "--", *CODE], cwd=str(VAULT),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20, creationflags=NO_WINDOW)
        if p.returncode != 0:
            return [(eid, "remote", "not a git repo")], {}
        lines = (p.stdout or "").splitlines()
        head = lines[0] if lines else ""
        m_a, m_b = re.search(r"ahead (\d+)", head), re.search(r"behind (\d+)", head)
        ahead, behind = m_a and m_a.group(1), m_b and m_b.group(1)
        # "[gone]": the fetch pruned the upstream branch (renamed or deleted on the host). No
        # counts, and nothing upstream holds this machine's commits, which is not "level".
        missing = "no upstream" if "..." not in head else "upstream gone" if "[gone]" in head else ""
        remote = str(CFG.get("push_remote") or "").strip()
        if remote:
            # vault_push pushes to push_remote and never sets an upstream. In a clone used as the
            # vault the upstream is the harness's origin, so the header counted every backed-up
            # commit as unpushed, one more each night. Measure against what is actually pushed to.
            git_ = lambda *a: subprocess.run(["git", *a], cwd=str(VAULT), capture_output=True, text=True,
                                             encoding="utf-8", errors="replace", timeout=20, creationflags=NO_WINDOW)
            br = git_("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            q = git_("rev-list", "--left-right", "--count", f"refs/remotes/{remote}/{br}...HEAD")
            if q.returncode == 0 and len(q.stdout.split()) == 2:
                behind, ahead = (n if n != "0" else None for n in q.stdout.split())
                missing = ""
            else:
                missing = f"not on {remote}/{br} yet (unpushed)"
        if missing:
            value = missing
        elif ahead and behind:
            value = f"diverged (ahead {ahead}, behind {behind})"
        elif ahead:
            value = f"ahead {ahead} (unpushed)"
        elif behind:
            value = "behind (pull needed)"
        else:
            value = "level"
        code = sorted(ln[3:].strip().strip('"') for ln in lines[1:] if ln.strip())
        if code:
            more = f" +{len(code) - 3} more" if len(code) > 3 else ""
            value += f"; uncommitted code: {', '.join(code[:3])}{more}"
        return [(eid, "remote", value)], {"head": head, "code": code}
    except Exception as e:
        return [(eid, "remote", f"unknown ({type(e).__name__})")], {}


def probe_command(spec: dict) -> tuple[list[tuple[str, str, str]], dict]:
    """A user-declared probe (palimpsest.json "probes"): run `cmd`, record its first line of
    output as entity.attr. Read-only by convention — a probe observes, it never changes anything."""
    eid, attr = spec["entity"], spec.get("attr", "status")
    enc = spec.get("encoding") or "utf-8"
    try:
        # Bytes, decoded strictly in the probe's declared encoding. Decoding as UTF-8 with
        # errors="replace" turned each letter a cp1251 probe printed into U+FFFD, so two values
        # of one length were the same string and a service going down was logged as "1 unchanged"
        # (review, 2026-10-02). Output that does not decode is a probe error, never a lossy value.
        p = subprocess.run(spec["cmd"], cwd=str(VAULT), capture_output=True,
                           timeout=spec.get("timeout", 20), creationflags=NO_WINDOW)
        if p.returncode == 0:
            # Only the recorded line has to decode: a probe whose first line is ASCII and whose
            # later lines are in a local code page recorded the right value before, and must not
            # turn into an error now. surrogateescape marks what does not decode, in any codec.
            out = p.stdout.decode(enc, "surrogateescape").strip().splitlines()
            first = out[0].strip() if out else "ok"
            if any("\udc80" <= ch <= "\udcff" for ch in first):
                return [(eid, attr, f"probe error (output not {enc})")], {"detail": f"stdout starts {p.stdout[:32].hex(' ')}"}
            return [(eid, attr, first[:200])], {}
        detail = (p.stderr.decode(enc, "backslashreplace") + p.stdout.decode(enc, "backslashreplace")).strip()[:200]
        fail = f"unreachable (rc {p.returncode})"
    except subprocess.TimeoutExpired:
        detail, fail = "timed out", "unreachable (timeout)"
    except Exception as e:
        # A fault on this machine, whatever the network is doing: mostly a command that never
        # started (not installed, not on PATH, not executable), but also a malformed spec (a
        # "timeout" that is not a number fails after the command was spawned, an "encoding" that
        # is not a str is a TypeError) or an "encoding" with no codec (LookupError). This branch
        # used to fall through to the network check: online that recorded the probe error anyway,
        # but offline, or with a network control nothing answers, a missing executable read as
        # "this machine's network is down" and nothing was recorded, run after run.
        return [(eid, attr, f"probe error ({type(e).__name__})")], {"detail": f"{type(e).__name__}: {e}"[:200]}
    if spec.get("network", True) and not local_network_up():
        return [], {"detail": detail, "local_network_down": True}
    return [(eid, attr, fail)], {"detail": detail}


BUILTIN = {"sync": probe_sync, "git": probe_git}


def cmd_probe(args):
    facts, _ = load_facts()
    cur = fold(facts)
    obs = load_observed(local_only=True)
    specs = {s["name"]: s for s in (CFG.get("probes") or []) if isinstance(s, dict) and s.get("name")}
    names = [n.strip() for n in args.only.split(",")] if args.only else list(BUILTIN) + list(specs)
    added, seen = 0, 0
    for name in names:
        spec = specs.get(name)
        if spec and not args.force and hours_since(obs.get(f"probe:{name}")) < spec.get("min_interval_h", 0):
            print(f"  {name}: skipped (probed {int(hours_since(obs.get(f'probe:{name}')) * 60)} min ago)")
            continue
        if name not in BUILTIN and not spec:
            print(f"  {name}: no such probe (built-in: {', '.join(BUILTIN)}; declared: {', '.join(specs) or 'none'})")
            continue
        try:
            triples, detail = BUILTIN[name]() if name in BUILTIN else probe_command(spec)
        except Exception as e:
            triples, detail = [], {"detail": f"{type(e).__name__}: {e}"[:200]}
            print(f"  {name}: probe error ({type(e).__name__})")
        _, ents, _ = load_entities()
        now = iso(utcnow())
        for eid, attr, value in triples:
            if eid not in ents:
                print(f"  probe {name}: {eid} is not registered — `state.py register {eid} --kind service`")
                continue
            rec = cur.get(eid, {}).get(attr)
            if rec and (rec.get("valid_from") or "") > now:
                # A later-dated fact outranks anything observed now: appending would not move the
                # current value, yet it printed "(was X)" and appended another superseded copy on
                # every run. Say what holds the key instead; nothing is recorded or marked seen.
                if rec.get("value") != value:
                    print(f"  {name}: observed {eid}.{attr} = {value}, but {rec.get('id')} (valid from "
                          f"{rec.get('valid_from')}) outranks it; the current value stays {render_value(rec)}. "
                          f"If that date is wrong, re-add the current value without --since.")
                continue
            if rec and rec.get("value") == value and rec.get("kind") == "observed":
                obs[f"{eid}.{attr}"] = now
                seen += 1
                continue
            f = make_fact(eid, attr, value, "observed", f"probe:{name}", [f"probe:{name}@{MACHINE}"])
            if append(f):
                added += 1
                cur.setdefault(eid, {})[attr] = {**f, "superseded": []}
                print(f"  {name}: {eid}.{attr} = {value}  (was {rec.get('value') if rec else '∅'})")
            obs[f"{eid}.{attr}"] = now
        if detail.get("local_network_down"):
            # nothing was learned about the target: keep the throttle open so the next run retries
            print(f"  {name}: skipped (this machine's network is down: {detail.get('detail', '')[:80]})")
        else:
            obs[f"probe:{name}"] = now
        if detail:
            obs[f"detail:{name}"] = detail
    save_observed(obs)
    refold()
    print(f"probe: {added} new fact(s), {seen} unchanged, probes run: {', '.join(names)}")


# ----------------------------------------------------------------------------- station lease (turn-based handoff)
def _lease(cur: dict) -> tuple[str, str | None]:
    rec = cur.get("station", {}).get("active")
    if not rec or rec.get("value") in (None, "free"):
        return "free", rec.get("valid_from") if rec else None
    return str(rec["value"]), rec.get("valid_from")


def _vault_push(*extra: str) -> int:
    p = subprocess.run([sys.executable, str(TOOLS / "vault_push.py"), *extra], cwd=str(VAULT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       creationflags=NO_WINDOW)
    for ln in (p.stdout or "").splitlines():
        print("  " + ln)
    return p.returncode


STATION_DESC = "which machine holds the vault for hand-written work"


def cmd_station(args):
    """One writer at a time for hand-written content when a vault is worked from two machines.
    The lease is a ledger fact, so every take, takeover and release is dated and sourced. `take`
    pulls first and pushes the lease; `release` pushes everything, then frees the lease. It is a
    convention the ledger records, not a lock git enforces: vault_push does not refuse commits."""
    facts, bad = load_facts()
    holder, since = _lease(fold(facts))
    if bad:
        print(damaged(bad))      # the holder comes from the fold too
    if args.action == "show":
        age = f" for {hours_since(since):.1f} h" if since and holder != "free" else ""
        print(f"station lease: {holder}{age} (this machine: {MACHINE})"
              + ("" if holder in ("free", MACHINE) else "  — hand-edit elsewhere only after `station take`"))
        return
    if args.action == "take":
        if not args.no_pull:
            print("pull first:")
            rc = _vault_push("--pull-only")
            if rc != 0:
                print("station take: the pull did not complete cleanly — resolve that first (see above)")
                sys.exit(1)
            facts, _ = load_facts()
            holder, since = _lease(fold(facts))
        # Registered only after the pull: written before it, an untracked State/entities.json on a
        # machine that had not yet pulled the registry made the pull refuse to overwrite it.
        ensure_entity("station", "flag", STATION_DESC)
        if holder == MACHINE:
            print(f"station: already held by this machine since {local(since)}")
        else:
            note = ""
            if holder != "free":
                age = hours_since(since)
                if age < LEASE_STALE_H and not args.force:
                    print(f"station: held by {holder} for {age:.1f} h (stale after {LEASE_STALE_H} h). "
                          f"Their unpushed work may exist — release it there, or `station take --force`.")
                    sys.exit(1)
                note = f"taken over from {holder} (lease age {age:.1f} h, {'forced' if args.force else 'stale'})"
            append(make_fact("station", "active", MACHINE, "asserted", f"station:take@{MACHINE}",
                             [f"station take on {MACHINE}"], None, note))
            refold()
            print(f"station: lease taken by {MACHINE}" + (f" — {note}" if note else ""))
        print("push:")
        rc = _vault_push(*(["--code"] if args.code else []))
        # The push rebased onto the remote first; a take from another machine that arrived there
        # and is dated later wins the fold, and this take would otherwise report success.
        now_holder, _ = _lease(fold(load_facts()[0]))
        if rc == 0 and now_holder != MACHINE:
            print(f"station: {now_holder} took the lease after this take (its take arrived in the push's rebase "
                  f"and is dated later) — {now_holder} holds it now; do not hand-edit here")
            sys.exit(1)
        sys.exit(rc)
    if args.action == "release":
        if holder not in (MACHINE, "free"):
            print(f"station: the lease is held by {holder}, not this machine — nothing to release")
            sys.exit(1)
        print("push everything first (lease still held here):")
        rc = _vault_push(*(["--code"] if args.code else []))
        if rc != 0:
            print("station release: the push did not complete — NOT releasing; fix the message above and rerun")
            sys.exit(rc)
        # Re-check after that push's rebase: the holder above came from this machine's ledger
        # before any pull, so a forced takeover from another machine was freed silently.
        holder, _ = _lease(fold(load_facts()[0]))
        if holder not in (MACHINE, "free"):
            print(f"station: {holder} took the lease over since this machine last pulled — NOT releasing "
                  f"(your work is pushed; the lease stays with {holder})")
            sys.exit(1)
        ensure_entity("station", "flag", STATION_DESC)
        append(make_fact("station", "active", "free", "asserted", f"station:release@{MACHINE}",
                         [f"station release on {MACHINE}"], None, f"released by {MACHINE}"))
        refold()
        print("push the release:")
        sys.exit(_vault_push())


# ----------------------------------------------------------------------------- lint
def cmd_lint(args):
    facts, bad = load_facts()
    kinds, ents, _ = load_entities()
    cur = fold(facts)
    obs = load_observed()
    unknown = sorted({f["entity"] for f in facts if f.get("entity") not in ents})
    conflicts = [(e, a) for e, recs in cur.items() for a, r in recs.items() if r.get("conflicts")]
    stale = [(e, a) for e, recs in cur.items() for a, r in recs.items() if is_stale(r, e, a, kinds, ents, obs)]
    nofacts = sorted(e for e in ents if e not in cur)
    ids = [f.get("id") for f in facts]
    dups = len(ids) - len(set(ids))
    badkind = [f.get("id") for f in facts if f.get("kind") not in KINDS]
    future = [(e, a, r) for e, recs in cur.items() for a, r in recs.items() if (r.get("valid_from") or "") > iso(utcnow())]
    for u in unknown:
        print(f"unknown entity in ledger: {u}")
    for e, a in conflicts:
        print(f"open contradiction: {e}.{a}")
    for e, a in stale:
        print(f"stale observation: {e}.{a} (last seen {local(last_seen(cur[e][a], e, a, obs))})")
    for e, a, r in future:
        print(f"future-dated: {e}.{a} = {render_value(r)} valid from {r.get('valid_from')} ({r.get('id')}) "
              f"outranks every observation until then")
    late = overdue(cur)
    for e, a, day, r in late:
        print(f"overdue: {e}.{a} names {day}, which has passed ({r.get('id')}): done? then append what happened")
    if bad:
        print(f"malformed lines: {bad}")
    if dups:
        # Identical lines, not conflicting ones: two writers appended the same observation in the
        # same second. The fold is unaffected; the usual cause is two copies of the sync running.
        print(f"duplicate ids: {dups} (two writers appended the same fact — is the sync running twice?)")
    if badkind:
        print(f"bad kinds: {badkind}")
    print(f"state: facts={len(facts)} entities={len(ents)} with-facts={len(cur)} unknown={len(unknown)} "
          f"conflicts={len(conflicts)} stale={len(stale)} no-facts={len(nofacts)} malformed={bad} dups={dups} "
          f"overdue={len(late)}")
    if args.verbose and nofacts:
        print("entities without facts: " + ", ".join(nofacts))
    write_outputs(cur, facts, kinds, ents, bad)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="State ledger: dated facts, deterministic fold, read-only probes.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="append a fact")
    a.add_argument("entity"); a.add_argument("attr"); a.add_argument("value")
    a.add_argument("--kind", choices=["decided", "asserted"], default="asserted")
    a.add_argument("--since", help="when it became true (valid_from); default now")
    a.add_argument("--future", action="store_true", help="allow a --since more than a few minutes ahead")
    a.add_argument("--source", action="append", help="[[Note]] or transcript/log ref; repeatable")
    a.add_argument("--by"); a.add_argument("--note")
    a.set_defaults(fn=cmd_add)

    s = sub.add_parser("show", help="current facts for an entity, or --hot")
    s.add_argument("entity", nargs="?"); s.add_argument("--history", action="store_true")
    s.add_argument("--json", action="store_true"); s.add_argument("--hot", action="store_true")
    s.add_argument("--opener", action="store_true", help="markdown block for the session opener")
    s.set_defaults(fn=cmd_show)

    f = sub.add_parser("fold", help="regenerate Register.md + current.json")
    f.set_defaults(fn=lambda args: (refold(), print(f"fold: wrote {REGISTER.relative_to(VAULT)} and {CURRENT.name}")))

    p = sub.add_parser("probe", help="observe what is observable; append facts only on change")
    p.add_argument("--only", help="comma list of probe names (built-in: sync,git; plus palimpsest.json probes)")
    p.add_argument("--force", action="store_true", help="ignore each declared probe's min_interval_h")
    p.set_defaults(fn=cmd_probe)

    r = sub.add_parser("retract", help="mark an attribute as no longer holding any value")
    r.add_argument("entity"); r.add_argument("attr"); r.add_argument("--since"); r.add_argument("--source", action="append")
    r.add_argument("--future", action="store_true", help="allow a --since more than a few minutes ahead")
    r.add_argument("--by"); r.add_argument("--note"); r.set_defaults(fn=cmd_retract)

    c = sub.add_parser("contradict", help="record an unordered conflict between two facts")
    c.add_argument("id_a"); c.add_argument("id_b"); c.add_argument("--reason", required=True)
    c.add_argument("--confidence", type=float, default=0.6); c.add_argument("--by"); c.set_defaults(fn=cmd_contradict)

    ac = sub.add_parser("accept", help="promote proposed facts into the ledger")
    ac.add_argument("id", nargs="?"); ac.add_argument("--all", action="store_true"); ac.set_defaults(fn=cmd_accept)

    rg = sub.add_parser("register", help="add or update an entity in the registry")
    rg.add_argument("id"); rg.add_argument("--kind"); rg.add_argument("--alias", action="append")
    rg.add_argument("--hot", action="store_true"); rg.add_argument("--stale-after", type=float)
    rg.add_argument("--desc"); rg.set_defaults(fn=cmd_register)

    stn = sub.add_parser("station", help="turn-based handoff: show | take [--force] [--code] | release [--code]")
    stn.add_argument("action", choices=["show", "take", "release"])
    stn.add_argument("--force", action="store_true", help="take a lease another machine holds (< LEASE_STALE_H old)")
    stn.add_argument("--code", action="store_true", help="also commit tools/ and root config (guards run)")
    stn.add_argument("--no-pull", action="store_true", help="take without pulling first (offline)")
    stn.set_defaults(fn=cmd_station)

    l = sub.add_parser("lint", help="unknown entities, open conflicts, stale observations, overdue next/deadline facts, duplicates")
    l.add_argument("--verbose", action="store_true"); l.set_defaults(fn=cmd_lint)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
