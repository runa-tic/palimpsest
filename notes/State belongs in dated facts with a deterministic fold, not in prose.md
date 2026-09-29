---
type: note
created: 2026-09-29
tags:
  - state
  - memory
  - reliability
---

# State belongs in dated facts with a deterministic fold, not in prose

Anything that describes the world *right now* — where a service runs, whether a timer is alive, which machine holds the vault — goes stale the moment the world moves, and prose has no way to notice. Record it as dated, append-only facts and compute the current view with a rule, not with judgment.

The failure that forced this: a paragraph in an always-loaded instructions file said a service stack was down. It was written in July. In September it outranked two current notes and raised a false alarm, because always-loaded text is read every session and nothing ever re-checks it. A memory file did the same with a person's travel: it asserted they were abroad for 47 days after they came home.

`tools/state.py` is the replacement. Each fact carries two clocks: `valid_from`, when it became true in the world, and `t`, when it was recorded. The fold is deterministic: per entity and attribute, the latest `valid_from` wins; older facts stay in the file as superseded; nothing is edited or deleted. Age never decides truth. An *observed* fact that has not been re-observed within `stale_after_h` is only flagged, never dropped. Probes observe what is observable and append only when a value changes, so a probe that runs every 15 minutes costs nothing on a quiet day. A conflict the fold cannot order is recorded as a `contradicts` fact with a reason and a confidence, for a human.

A survey of nine agent-memory systems found the same shape in the one rigorous design (a bi-temporal graph that stamps old facts invalid rather than deleting them) and its absence nearly everywhere else. Write-time LLM adjudication, where a model decides which fact to overwrite, was being retreated from.

An audit a month in found three ways the ledger itself lied, all fixed:

- **Last-seen was per machine.** The time an observation was last confirmed lived in a gitignored file, so each machine saw the other's healthy, unchanged observations as stale — for a week. Last-seen is now committed per machine, hour-floored so it changes at most hourly, and merged on read.
- **The git probe only looked for bad news.** It searched for "behind", so a checkout that was ahead (unpushed) or dirty (uncommitted code) read "level", while a fix sat uncommitted for three days. It now reports ahead, behind or diverged, and names uncommitted code.
- **A laptop's outage became a fact about a server.** Six of seven "server unreachable" facts in a week came from runs where the laptop's own DNS had failed. A failed probe now records nothing unless a control check shows this machine is online. The control has to be a *verified TLS handshake*, not a TCP connect: behind a TUN-mode proxy or VPN, a TCP connect to an unroutable address succeeds locally in 10 ms, so a connect-based check said "online" through the very outage it was for.

The general rule: a fact about the present needs a date, a source, and a way to be superseded that does not involve editing the old claim. And every health check needs a test in which the thing is actually broken; the three bugs above all read green.

## Related
- A union merge keeps both copies when two machines render the same block
- Daily-note briefing blocks are regenerated output, so durable edits go in the roll source
