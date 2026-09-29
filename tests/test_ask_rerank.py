"""ask.py's default path — hybrid retrieval, then a language-routed cross-encoder rerank — with
no model call (--retrieve-only), plus the fallback: if a reranker cannot load, ask.py says so
and the sources line does NOT claim "+rerank".

Needs sentence-transformers and the models (downloads ~0.6 GB on first run unless cached);
without the package it reports SKIP rather than failing.
"""
import importlib.util, os, sys
from _util import Checks, make_vault, run, write

NOTES = {
    "Restart budgets trap a crashing service": "A supervisor with a restart budget stops retrying after N crashes, "
        "so a service that crashes at boot stays down until someone intervenes by hand.",
    "Config read at startup needs a restart to change": "Settings read once at process start only take effect "
        "after a restart; reading them per request lets you change them live without downtime.",
    "Sourdough needs a long cold proof": "Bread dough left overnight in the fridge develops flavour and structure.",
    "Union merges keep both copies": "A git union merge keeps both sides' lines, so two renders of one block both survive.",
}


def main() -> int:
    c = Checks("ask.py: hybrid + rerank")
    if importlib.util.find_spec("sentence_transformers") is None:
        print("SKIP: sentence-transformers not installed (pip install sentence-transformers)")
        return 0
    v = make_vault()
    for title, body in NOTES.items():
        write(v, f"10 Notes/{title}.md", f"# {title}\n\n{body}\n")
    env = {"CLAUDE_BRAIN_NO_HOOK": "1"}

    r = run(v, "ask.py", "--retrieve-only", "can I change a setting without restarting the service?", env=env)
    lines = [l.strip() for l in r.stdout.splitlines()]
    c.ok("sources (hybrid+rerank):" in r.stdout, "English question: the rerank ran", r.stdout + r.stderr[-400:])
    c.ok(len(lines) > 1 and "Config read at startup" in lines[1], "...and the right note is first", r.stdout)

    r = run(v, "ask.py", "--retrieve-only", "можно ли поменять настройку без перезапуска сервиса?", env=env)
    lines = [l.strip() for l in r.stdout.splitlines()]
    c.ok("sources (hybrid+rerank):" in r.stdout, "Russian question: the multilingual rerank ran", r.stdout + r.stderr[-400:])
    c.ok(len(lines) > 1 and "Config read at startup" in lines[1], "...and the right note is first", r.stdout)

    ask_py = (v / "tools" / "ask.py").read_text()
    (v / "tools" / "ask.py").write_text(ask_py.replace('"en": "cross-encoder/ms-marco-MiniLM-L-6-v2"',
                                                      '"en": "no-such-org/no-such-model"'))
    r = run(v, "ask.py", "--retrieve-only", "can I change a setting without restarting?",
            env={**env, "HF_HUB_OFFLINE": "1"})
    c.ok("rerank: skipped" in r.stderr and "sources (hybrid):" in r.stdout,
         "a reranker that cannot load falls back to hybrid and says so", r.stdout + r.stderr[-300:])
    r = run(v, "ask.py", "--retrieve-only", "--no-rerank", "restart budget", env=env)
    c.ok("sources (hybrid):" in r.stdout, "--no-rerank keeps the hybrid order", r.stdout)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
