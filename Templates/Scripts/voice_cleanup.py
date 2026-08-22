#!/usr/bin/env python3
"""
Voice Cleanup: Watches the local VoiceInput drop folder for raw dictation
.txt files, sends them to Claude for cleanup, and saves polished markdown
notes to the Creations folder in your Obsidian vault.

Usage:
    python3 Templates/Scripts/voice_cleanup.py              # watch mode
    python3 Templates/Scripts/voice_cleanup.py --once       # process and exit

Setup:
    1. pip install -r Templates/Scripts/requirements.txt
    2. Create an iOS Shortcut (see docs/Voice Note to Obsidian Guide.md)
    3. Edit the config section below to match your paths
    4. Set ANTHROPIC_API_KEY in ~/dev/secrets/.env (or your preferred location).
       To route through an institutional AI gateway instead, set LLM_BASE_URL
       and LLM_API_KEY_NAME — see Templates/Scripts/llm_endpoint.py.

Requires:
    pip install anthropic pyyaml python-dotenv
"""

from __future__ import annotations

import os
import sys
import time
import argparse
import subprocess
import logging
from pathlib import Path
from datetime import datetime

import anthropic
import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voice_cleanup")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load API keys from the canonical secrets file — edit this path if needed
load_dotenv(Path.home() / "dev" / "secrets" / ".env")

# Vault root is two levels up from Templates/Scripts/
VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()

# Config file location
CONFIG_PATH = Path(__file__).parent / "voice_cleanup_config.yaml"


def _default_watch_folder() -> str:
    """Default dictation inbox.

    Resolved by source_media.drop_dir so the watchers and the mail transport
    can't disagree about where drops live — ~/SourceMedia/VoiceInput.
    Override in voice_cleanup_config.yaml to point somewhere else.
    """
    import source_media
    return str(source_media.drop_dir("voice"))


def load_config() -> dict:
    """Load configuration, with sensible defaults if no config file exists."""
    defaults = {
        "claude_model": "claude-sonnet-4-6",
        "vault_path": str(VAULT_ROOT),
        "watch_folder": _default_watch_folder(),
    }

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        defaults.update(cfg)

    defaults["vault_path"] = str(Path(defaults["vault_path"]).expanduser())
    defaults["watch_folder"] = str(Path(defaults["watch_folder"]).expanduser())
    return defaults


# ---------------------------------------------------------------------------
# Claude cleanup
# ---------------------------------------------------------------------------

def clean_transcription(client: anthropic.Anthropic, raw_text: str, model: str) -> str:
    """Send raw dictation text to Claude for cleanup."""
    log.info("Sending to Claude for cleanup (%d chars)...", len(raw_text))

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": (
                    "You are cleaning up a raw voice transcription. "
                    "Fix grammar, remove false starts and filler words, "
                    "organize into clear paragraphs, but preserve the "
                    "speaker's meaning and tone exactly. Do NOT add any "
                    "commentary, headers, bullet points, or metadata — "
                    "output ONLY the cleaned-up text.\n\n"
                    f"Raw transcription:\n\n{raw_text}"
                ),
            }
        ],
    )
    try:
        import usage_log
        usage_log.record("voice_cleanup", model, response.usage)
    except Exception:
        pass
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def process_file(txt_path: Path, client: anthropic.Anthropic, cfg: dict):
    """Process one raw dictation file."""
    vault = Path(cfg["vault_path"])
    creations = vault / "Creations"
    creations.mkdir(exist_ok=True)

    # Force iCloud to download the file locally before reading
    try:
        subprocess.run(["brctl", "download", str(txt_path)], timeout=10, capture_output=True)
    except Exception:
        pass  # brctl may not exist on all macOS versions

    # Read raw text — retry if iCloud is still syncing
    raw_text = None
    for attempt in range(10):
        try:
            raw_text = txt_path.read_text(encoding="utf-8").strip()
            break
        except OSError as e:
            log.warning("File not ready (attempt %d/10): %s — %s", attempt + 1, txt_path.name, e)
            time.sleep(5)
    if raw_text is None:
        log.error("Could not read file after 10 attempts, skipping: %s", txt_path.name)
        return
    if not raw_text:
        log.warning("Empty file, skipping: %s", txt_path.name)
        txt_path.unlink()
        return

    log.info("Processing: %s", txt_path.name)

    # Clean up with Claude
    cleaned = clean_transcription(client, raw_text, cfg["claude_model"])

    # Build the note
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")

    note_content = f"""---
created: {date_str}T{time_str}
source: voice-note
tags: []
---

{cleaned}
"""

    # Save to Creations
    note_path = creations / f"{timestamp}.md"
    note_path.write_text(note_content, encoding="utf-8")
    log.info("Created note: %s", note_path.name)

    # Remove the raw input file
    txt_path.unlink()
    log.info("Removed raw file: %s", txt_path.name)

    print(f"  Created: {note_path.name}")
    print(f"     Folder: Creations/")
    print(f"     ({len(raw_text)} chars raw -> {len(cleaned)} chars cleaned)\n")


def get_pending_files(inbox: Path) -> list[Path]:
    """Return .txt files in the inbox folder."""
    if not inbox.exists():
        return []
    return sorted(
        f for f in inbox.iterdir()
        if f.suffix.lower() == ".txt" and not f.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Voice note cleanup processor")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending files once and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Seconds between checks in watch mode (default: 10)",
    )
    args = parser.parse_args()

    # Load config
    cfg = load_config()

    # Resolve the endpoint and its credential (env/.env first, then the
    # platform keystore). Stock Anthropic unless LLM_BASE_URL redirects it.
    import llm_endpoint
    try:
        client = llm_endpoint.client()
    except llm_endpoint.EndpointError as exc:
        log.error("%s", exc)
        sys.exit(1)
    log.info("Endpoint: %s", llm_endpoint.describe())

    # Ensure inbox folder exists
    inbox = Path(cfg["watch_folder"])
    inbox.mkdir(parents=True, exist_ok=True)
    vault = Path(cfg["vault_path"])

    if args.once:
        files = get_pending_files(inbox)
        if not files:
            log.info("No pending voice notes in %s", inbox)
            return
        for f in files:
            process_file(f, client, cfg)
        log.info("Done — processed %d file(s).", len(files))
    else:
        print(f"\nVoice Cleanup is running.")
        print(f"   Watching: {inbox}")
        print(f"   Notes saved to: {vault / 'Creations'}")
        print(f"   Press Ctrl+C to stop.\n")
        try:
            while True:
                files = get_pending_files(inbox)
                for f in files:
                    process_file(f, client, cfg)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
