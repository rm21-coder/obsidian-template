#!/usr/bin/env python3
"""rag_status.py -- is the optional local-LLM RAG layer set up on this machine?

Most installs never run the local RAG stack (Ollama + Open WebUI + the nightly
obsidian-rag-sync). Things that only make sense with it -- the dashboard's
Refresh RAG index button and its RAG sync card, the rag-sync scheduled job on
Windows -- ask here, so they all agree.

The test: OBSIDIAN_COLLECTION_ID is set, in the environment or in
~/dev/secrets/.env (%USERPROFILE%\\dev\\secrets\\.env on Windows). The sync
cannot run without it, and it is filled in only while setting RAG up
(installer component 50-llm-rag / setup-rag.ps1). It is an identifier, not
a credential, so reading it pulls no secret anywhere. An empty value -- what
the installers' stubs write -- or a placeholder like <id> does not count.

Deliberately not "is Open WebUI answering right now": a button that came and
went with Docker would be worse than one that reports a failed run.

Stdlib only, and Python 3.9-safe: the dashboard runs under /usr/bin/python3 on
macOS, which has no python-dotenv.

    python3 rag_status.py      exit 0 if set up, 1 if not (prints which)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

KEY = "OBSIDIAN_COLLECTION_ID"

# Path.home() is %USERPROFILE% on Windows, so this is both platforms' file.
SECRETS_ENV = Path(os.environ.get("RAG_STATUS_SECRETS_ENV",
                                  str(Path.home() / "dev" / "secrets" / ".env")))

_LINE = re.compile(r"^\s*(?:export\s+)?" + KEY + r"\s*=\s*(.*?)\s*$")


def _usable(value: str | None) -> bool:
    v = (value or "").strip().strip('"').strip("'").strip()
    return bool(v) and not any(c in v for c in "<> \t")


def collection_id_from_env_file(path: Path | None = None) -> str | None:
    try:
        text = (path or SECRETS_ENV).read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if m:
            value = m.group(1)
            if "#" in value and not value.startswith(("'", '"')):
                value = value.split("#", 1)[0]             # trailing comment
            return value.strip().strip('"').strip("'").strip()
    return None


def configured(path: Path | None = None) -> bool:
    """True when this machine has the RAG layer set up."""
    if _usable(os.environ.get(KEY)):
        return True
    return _usable(collection_id_from_env_file(path))


def main() -> int:
    ok = configured()
    print("RAG is set up on this machine" if ok
          else f"RAG is not set up here ({KEY} is not set)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
