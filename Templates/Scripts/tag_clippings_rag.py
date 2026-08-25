#!/usr/bin/env python3
"""
tag_clippings_rag.py — Retrieval-augmented variant of tag_clippings.py.

Instead of putting your whole tag allowlist into one model call, this ranks the
taxonomy by semantic relevance to each note using LOCAL embeddings (Ollama), then
hands Claude a short, focused candidate list -- each with a one-line gloss so the
model interprets the tag the way you intend. This improves precision on long,
multi-topic notes, where a single pass over a large flat list tends to satisfice
on broad tags and miss the specific child tags.

Design choices worth knowing:
  - AUGMENT, DON'T GATE. Candidates = (top-N by embedding) UNION (the note's
    current tags), so retrieval can only ADD options, never hide an existing one.
  - ADDITIVE mode (--additive) never removes a tag a note already has; good for
    enriching already-reviewed notes. A cap (--max-add) keeps only the strongest
    N additions by similarity, so reviewed notes gain a few precise tags, not many.
  - SURGICAL writes: only the frontmatter tags: block is rewritten; bodies and
    other frontmatter stay byte-for-byte identical. Every --apply run writes a
    rollback manifest; undo with --rollback.

Privacy: embeddings run locally via Ollama; the only cloud call is the final
tagging request to Anthropic (same as tag_clippings.py).

Usage:
    # preview (writes nothing):
    python3 Templates/Scripts/tag_clippings_rag.py --dry-run --additive 'Clippings'
    # apply, only ever adding up to 3 tags per note:
    python3 Templates/Scripts/tag_clippings_rag.py --apply --additive 'Knowledge'
    # undo a prior apply:
    python3 Templates/Scripts/tag_clippings_rag.py --rollback tag_rag_manifest_*.json

Setup:
    1. pip install anthropic pyyaml python-dotenv requests
    2. Ollama running locally with an embedding model: ollama pull nomic-embed-text
    3. ANTHROPIC_API_KEY in your environment or ~/dev/secrets/.env (see below),
       or LLM_BASE_URL + LLM_API_KEY_NAME to route through an institutional
       AI gateway — see Templates/Scripts/llm_endpoint.py.
    4. Provide glosses in tag_glosses.py (ships with an example to replace).
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ---------- venv bootstrap --------------------------------------------------
#
# The shebang is `/usr/bin/env python3`, so the interpreter is whatever the
# caller's PATH hands us. A launcher with the stock macOS PATH -- an Obsidian
# plugin, a launchd job, a .app wrapper -- lands on /usr/bin/python3 (system
# Python 3.9, no third-party packages) and dies on the imports below. Re-exec
# into the sibling venv unless we're already inside one. Path is derived from
# __file__, so it survives the genericized repo copy. The sentinel keeps a
# broken venv from looping forever.
_HERE = Path(__file__).resolve()
_VENV_PY = _HERE.parent / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python3"
)
# __name__ guard first, and it is load-bearing. os.execv REPLACES the running
# process, so at module scope this fires on `import` -- and under pytest the
# process it replaces is pytest. Importing this module while collecting its
# test file therefore killed the runner before it could report: the documented
# entry point returned exit 2 with zero test output, on any machine where the
# vault venv existed. All 599 tests were unreachable and the failure read like
# a pytest config problem. Found on a Mac Studio full-cycle run 2026-08-25 and
# reproduced on the primary machine by creating the venv.
#
# Re-execing is still right when this file is run as a command; it is never
# right on import. Pinned by
# test_static.py::TestNoModuleReExecsOnImport.
if (
    __name__ == "__main__"
    and sys.prefix == sys.base_prefix
    and _VENV_PY.exists()
    and not os.environ.get("_VENV_BOOTSTRAPPED")
):
    os.environ["_VENV_BOOTSTRAPPED"] = "1"
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(_HERE), *sys.argv[1:]])

import requests

try:
    import yaml
except ImportError:
    sys.exit("pip install pyyaml")

from dotenv import load_dotenv
load_dotenv(Path.home() / "dev" / "secrets" / ".env")   # edit to match your secrets

from tag_glosses import gloss_for                         # ships alongside this script

# ─── Configuration ───────────────────────────────────────────────────────────
VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()   # two levels up from Templates/Scripts/
TAXONOMY_FILE = VAULT_ROOT / "Knowledge" / "Tag Taxonomy.md"
EMBED_CACHE = VAULT_ROOT / "Templates" / "Scripts" / ".tag_embeddings_cache.json"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("TAG_EMBED_MODEL", "nomic-embed-text")
CLAUDE_MODEL = "claude-sonnet-4-6"

TOP_N = 30                 # candidates shown to the model (of the full taxonomy)
MAX_CONTENT_CHARS = 6000
ADMIN_TAGS = {"clippings", "task"}   # structural tags; never proposed by the model

# A named-entity tag survives only if the entity actually appears in the body.
# Stops an AI/cloud-heavy corpus from over-applying vendor/product tags to notes
# that merely discuss the general field. Extend for your taxonomy.
NAMED_ENTITY_ALIASES = {
    "Vendors/AWS": ["aws", "amazon web services", "amazon"],
    "Vendors/Azure": ["azure"],
    "Vendors/GCP": ["gcp", "google cloud"],
    "Vendors/Google": ["google", "gemini"],
    "Vendors/Microsoft": ["microsoft", "m365", "copilot", "windows", "entra", "azure"],
    "Vendors/NVIDIA": ["nvidia", "cuda"],
    "Vendors/OpenAI": ["openai", "chatgpt", "gpt-"],
    "Vendors/Anthropic": ["anthropic"],
    "Vendors/ServiceNow": ["servicenow", "service now"],
    "Vendors/Salesforce": ["salesforce"],
    "AI/Claude": ["claude", "anthropic"],
    "AI/Copilot": ["copilot"],
}


# ─── Taxonomy + frontmatter helpers ──────────────────────────────────────────
def parse_taxonomy(path: Path) -> list[str]:
    tags, stop = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Filtered out") or line.startswith("## Merged into"):
            stop = True            # audit sections below are not the allowlist
        if stop:
            continue
        m = re.match(r"^-\s+([A-Za-z][A-Za-z0-9 _/\-]*?)\s*$", line)
        if m:
            t = m.group(1).strip()
            if t and t not in tags:
                tags.append(t)
    return tags


def parse_frontmatter(text: str):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None, text
    return fm, m.group(2)


_FM_SPLIT = re.compile(r"^(---\s*\n)(.*?)(\n---\s*\n?)(.*)$", re.DOTALL)


def set_tags_in_text(text: str, new_tags: list[str]):
    """Surgically replace ONLY the frontmatter tags: block, leaving every other
    byte unchanged. Handles block-list and inline forms. Returns None if the file
    has no frontmatter (caller falls back)."""
    m = _FM_SPLIT.match(text)
    if not m:
        return None
    open_, fm, close, body = m.groups()
    lines = fm.split("\n")
    for i, l in enumerate(lines):
        if re.match(r"^tags:\s*$", l):                       # block-list form
            j = i + 1
            while j < len(lines) and re.match(r"^\s*-\s+.+", lines[j]):
                j += 1
            indent_m = re.match(r"^(\s*)-", lines[i + 1]) if j > i + 1 else None
            indent = indent_m.group(1) if indent_m else "  "
            block = [lines[i]] + [f"{indent}- {t}" for t in new_tags]
            return open_ + "\n".join(lines[:i] + block + lines[j:]) + close + body
        m2 = re.match(r"^tags:\s*\[(.*)\]\s*$", l)            # inline form
        if m2:
            quoted = '"' in m2.group(1) or "'" in m2.group(1)
            items = [f'"{t}"' for t in new_tags] if quoted else list(new_tags)
            lines[i] = f"tags: [{', '.join(items)}]"
            return open_ + "\n".join(lines) + close + body
    block = ["tags:"] + [f"  - {t}" for t in new_tags]       # no tags: key -> insert
    return open_ + "\n".join(block + lines) + close + body


def clean_body(body: str) -> str:
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", body)   # images
    body = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", body)    # inline links incl. anchor text
    body = re.sub(r"https?://\S+", " ", body)            # bare urls
    return body[:MAX_CONTENT_CHARS]


# ─── Local embeddings via Ollama ─────────────────────────────────────────────
def embed(text: str) -> list[float]:
    r = requests.post(f"{OLLAMA_URL}/api/embeddings",
                      json={"model": EMBED_MODEL, "prompt": text}, timeout=60)
    r.raise_for_status()
    return r.json()["embedding"]


def cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


def load_tag_embeddings(tags: list[str]) -> dict:
    cache = {}
    if EMBED_CACHE.exists():
        try:
            cache = json.loads(EMBED_CACHE.read_text())
        except json.JSONDecodeError:
            cache = {}
    out, dirty = {}, False
    for t in tags:
        key = f"{t}::{gloss_for(t)}"
        if key in cache:
            out[t] = cache[key]
        else:
            out[t] = embed(f"{t}. {gloss_for(t)}")
            cache[key] = out[t]
            dirty = True
    if dirty:
        EMBED_CACHE.write_text(json.dumps(cache))
    return out


def candidate_tags(body: str, tags, tag_emb, current: list[str]):
    """top-N by similarity, UNIONED with current tags. Returns (candidates, bvec)."""
    bvec = embed(clean_body(body))
    ranked = sorted(((t, cosine(bvec, tag_emb[t])) for t in tags),
                    key=lambda x: x[1], reverse=True)
    cands = [t for t, _ in ranked[:TOP_N]]
    for t in current:
        if t in tags and t not in cands:
            cands.append(t)
    return cands, bvec


# ─── Guardrails ──────────────────────────────────────────────────────────────
def entity_supported(tag: str, body_lower: str) -> bool:
    aliases = NAMED_ENTITY_ALIASES.get(tag)
    return True if aliases is None else any(a in body_lower for a in aliases)


def drop_redundant_parents(tags: list[str]) -> list[str]:
    return [t for t in tags
            if "/" in t or not any(o.startswith(t + "/") for o in tags)]


def messages_create_retry(client, **kwargs):
    """messages.create with exponential backoff on rate-limit / overload errors."""
    delay = 2.0
    for attempt in range(7):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            transient = (type(e).__name__ in ("RateLimitError", "APITimeoutError",
                         "APIConnectionError", "InternalServerError", "OverloadedError")
                         or "429" in msg or "529" in msg or "overloaded" in msg
                         or "rate limit" in msg)
            if not transient or attempt == 6:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 60)


# ─── Claude call over the focused candidate list ─────────────────────────────
def suggest_tags(client, title, body, candidates: list[str]) -> list[str]:
    catalog = "\n".join(f"- {t}: {gloss_for(t)}" for t in candidates)
    prompt = f"""You are a semantic tagger for an Obsidian knowledge vault. Assign the
most relevant tags to this note. You may ONLY choose from the CANDIDATE TAGS below
(they were pre-selected as the most relevant of the full taxonomy). Each candidate
has a short gloss describing what it means in THIS vault -- use the gloss, not your
own reading of the tag name.

RULES:
1. Choose ONLY from the candidate list. Never invent tags.
2. Be conservative. Assign only tags that are clearly and substantially supported
   by the body -- a tag should reflect a main subject of the note, not a passing
   mention. Prefer a few precise tags over many marginal ones; 1-2 good tags beats
   6 loose ones.
3. Prefer the most specific applicable tag (e.g. AI/Agents over AI).
4. Tag the substantive subject matter of the BODY only -- not the file format,
   not the source, not anything outside the body text.
5. A specific vendor/product/topic tag that the note clearly discusses should be
   included even if a broader tag already covers the area.
6. Do NOT add a parent tag (e.g. AI) if you are also selecting a more
   specific child of it (e.g. AI/Agents). Keep only the child.
7. Only assign a Vendors/* tag, or a product-specific tag, if the note NAMES that
   specific vendor or product. Do not apply it just because the note is about the
   general field (AI, cloud, etc.).

CANDIDATE TAGS:
{catalog}

NOTE TITLE: {title}

NOTE BODY:
{clean_body(body)}

Respond with ONLY a JSON array of tag strings, e.g. ["AI/Agents", "Vendors/Microsoft"].
"""
    resp = messages_create_retry(client, model=CLAUDE_MODEL, max_tokens=200,
                                 messages=[{"role": "user", "content": prompt}])
    try:
        import usage_log
        usage_log.record("tag_clippings_rag", CLAUDE_MODEL, resp.usage)
    except Exception:
        pass
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    try:
        tags = json.loads(m.group(0) if m else text)
    except (json.JSONDecodeError, AttributeError):
        return []
    cand_set = set(candidates)
    return [t for t in tags if t in cand_set]    # hard allowlist guard


# ─── Per-note processing ─────────────────────────────────────────────────────
def process(path: Path, client, tags, tag_emb, apply: bool, additive: bool, max_add: int):
    raw = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)
    if fm is None:
        print(f"  ! skip (no frontmatter): {path.name}")
        return None
    if not body.strip():
        print(f"  . skip (empty body): {path.name}")
        return None

    current = [t for t in (fm.get("tags") or []) if t not in ADMIN_TAGS]
    admin = [t for t in (fm.get("tags") or []) if t in ADMIN_TAGS]
    cands, bvec = candidate_tags(body, tags, tag_emb, current)
    proposed = suggest_tags(client, fm.get("title", path.stem), body, cands)

    body_lower = clean_body(body).lower()
    guarded = [t for t in proposed if t in current or entity_supported(t, body_lower)]
    proposed = drop_redundant_parents(guarded)

    if additive:
        new_adds = [t for t in proposed if t not in current]
        pool = current + new_adds
        new_adds = [t for t in new_adds
                    if "/" in t or not any(o != t and o.startswith(t + "/") for o in pool)]
        if max_add and len(new_adds) > max_add:
            new_adds = sorted(new_adds, key=lambda t: cosine(bvec, tag_emb[t]),
                              reverse=True)[:max_add]
        final = current + new_adds
    else:
        final = proposed

    added = [t for t in final if t not in current]
    dropped = [] if additive else [t for t in current if t not in final]

    print(f"\n  {path.name}")
    print(f"    current : {current}")
    print(f"    final   : {final}")
    if added:
        print(f"    + added : {added}")
    if dropped:
        print(f"    - review (current tag not re-proposed): {dropped}")

    new_tags = admin + final
    if new_tags == admin + current or not apply:
        return None

    new_text = set_tags_in_text(raw, new_tags)
    if new_text is None:
        fm["tags"] = new_tags
        new_text = ("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                    + "\n---\n" + body)
    path.write_text(new_text, encoding="utf-8")
    print(f"    >> written")
    return {"path": str(path), "rel": str(path.relative_to(VAULT_ROOT)),
            "added": added, "original": raw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="*", default=["Clippings"], help="vault subfolders")
    ap.add_argument("--apply", action="store_true", help="write tags to files")
    ap.add_argument("--dry-run", action="store_true", help="print only (default)")
    ap.add_argument("--additive", action="store_true",
                    help="only ADD tags; never remove a tag the note already has")
    ap.add_argument("--max-add", type=int, default=3,
                    help="max NEW tags per note in additive mode (0 = no cap)")
    ap.add_argument("--limit", type=int, default=0, help="cap files processed")
    ap.add_argument("--rollback", metavar="MANIFEST",
                    help="undo a prior --apply from its manifest, then exit")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    if args.rollback:
        manifest = json.loads(Path(args.rollback).read_text())
        for rec in manifest["changes"]:
            Path(rec["path"]).write_text(rec["original"], encoding="utf-8")
        print(f"Rolled back {len(manifest['changes'])} files from {args.rollback}")
        return

    import llm_endpoint
    try:
        client = llm_endpoint.client()
    except llm_endpoint.EndpointError as exc:
        sys.exit(f"Error: {exc}")
    except ImportError:
        sys.exit("pip install anthropic")
    print(f"Endpoint: {llm_endpoint.describe()}")

    tags = [t for t in parse_taxonomy(TAXONOMY_FILE) if t not in ADMIN_TAGS]
    print(f"Taxonomy: {len(tags)} candidate tags. Embedding (cached) via {EMBED_MODEL}...")
    tag_emb = load_tag_embeddings(tags)
    cap = f" | max +{args.max_add}/note" if (args.additive and args.max_add) else ""
    print(f"Mode: {'APPLY (writing files)' if apply else 'DRY-RUN (no writes)'}"
          f"{' | ADDITIVE' if args.additive else ''}{cap}\n")

    files = []
    for folder in (args.folders or ["Clippings"]):
        for p in sorted((VAULT_ROOT / folder).rglob("*.md")):
            if any(part.startswith(".") for part in p.relative_to(VAULT_ROOT).parts):
                continue
            files.append(p)
    if args.limit:
        files = files[:args.limit]

    from datetime import datetime
    manifest = Path(f"tag_rag_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    changes = []

    def flush():
        if apply and changes:
            manifest.write_text(json.dumps({"changes": changes}, ensure_ascii=False, indent=0))

    try:
        for f in files:
            try:
                rec = process(f, client, tags, tag_emb, apply, args.additive, args.max_add)
                if rec:
                    changes.append(rec)
                    if apply and len(changes) % 20 == 0:
                        flush()                      # periodic crash-safe checkpoint
            except Exception as e:
                print(f"  ! error on {f.name}: {e}")
    except KeyboardInterrupt:
        print("\n[interrupted] flushing rollback manifest for changes so far...")
    finally:
        flush()

    if apply:
        if changes:
            print(f"\nApplied to {len(changes)} notes. Rollback manifest: {manifest}")
            print(f"Undo with:  python3 tag_clippings_rag.py --rollback {manifest}")
        else:
            print("\nApplied: 0 notes changed.")


if __name__ == "__main__":
    main()
