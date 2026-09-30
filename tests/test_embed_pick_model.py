"""embed.pick_model: the default model is multilingual-e5-base, but a query only uses it once its
index covers READY of the vault; before that it answers with e5-small and returns a hint, so the
first question on a machine that has not built the big index does not stall for hours while
Index.open builds it. An explicit BRAIN_EMBED_MODEL is never second-guessed.

Works from fake manifests only: no sentence-transformers, no model download.
"""
import importlib, json, os, sys
from _util import Checks, make_vault, write


def manifest(v, model: str, rels: list[str], chunk_chars: int):
    import embed
    d = v / "tools" / "cache" / f"embed-{embed._slug(model)}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"model": model, "chunk_chars": chunk_chars,
                                                 "files": {r: {"sha": "x", "spans": []} for r in rels},
                                                 "rows": []}), encoding="utf-8")


def fresh(v):
    sys.path.insert(0, str(v / "tools"))
    for m in ("embed",):
        sys.modules.pop(m, None)
    import embed
    return importlib.reload(embed)


def main() -> int:
    c = Checks("embed: pick_model")
    os.environ.pop("BRAIN_EMBED_MODEL", None)
    v = make_vault()
    rels = [f"10 Notes/n{i}.md" for i in range(20)]
    for r in rels:
        write(v, r, "# n\n\nbody\n")
    files = [v / r for r in rels]

    embed = fresh(v)
    c.ok(embed.MODEL == embed.PREFERRED == "intfloat/multilingual-e5-base", "default model is e5-base", embed.MODEL)

    hint = embed.pick_model(files)
    c.ok(embed.MODEL == embed.FALLBACK and hint and "0%" in hint,
         "no index at all -> e5-small with a hint", f"{embed.MODEL} / {hint}")

    manifest(v, embed.PREFERRED, rels[:10], embed.CHUNK_CHARS)
    hint = embed.pick_model(files)
    c.ok(embed.MODEL == embed.FALLBACK and hint and "50%" in hint,
         "half-built e5-base index -> still e5-small", f"{embed.MODEL} / {hint}")

    manifest(v, embed.PREFERRED, rels[:19], embed.CHUNK_CHARS)
    hint = embed.pick_model(files)
    c.ok(embed.MODEL == embed.PREFERRED and hint is None, "95% covered -> e5-base, no hint", f"{embed.MODEL} / {hint}")

    manifest(v, embed.PREFERRED, rels, embed.CHUNK_CHARS + 1)
    embed.pick_model(files)
    c.ok(embed.MODEL == embed.FALLBACK, "a manifest from other chunking settings counts as not built", embed.MODEL)

    os.environ["BRAIN_EMBED_MODEL"] = "intfloat/multilingual-e5-small"
    embed = fresh(v)
    hint = embed.pick_model(files)
    c.ok(embed.MODEL == "intfloat/multilingual-e5-small" and hint is None, "BRAIN_EMBED_MODEL pins the model",
         f"{embed.MODEL} / {hint}")
    os.environ.pop("BRAIN_EMBED_MODEL", None)
    return c.done()


if __name__ == "__main__":
    sys.exit(main())
