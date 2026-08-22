#!/usr/bin/env python3
"""usage_log.py — per-pipeline LLM token accounting.

Every script that calls a hosted model appends one JSON line per API call:

    {"ts": ..., "pipeline": "tag_clippings", "model": "...",
     "input_tokens": N, "output_tokens": N,
     "cache_creation_input_tokens": N, "cache_read_input_tokens": N}

The morning dashboard reads the last N days and renders tokens per pipeline
plus a dollar figure estimated from Anthropic list prices. The estimate is
labeled as such — an install running through a gateway (Bedrock, LiteLLM,
an institutional proxy) pays whatever its contract says, but relative cost
per pipeline is still the number that tells you where to optimize.

The log lives in the runtime state dir, not the vault: it is telemetry, not
content, and it must never sync to other devices or appear in a vault search.
Failures are swallowed — usage accounting must never break a pipeline.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path


def _state_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / "obsidian-usage"
    return Path.home() / ".local" / "share" / "obsidian-usage"


def log_path() -> Path:
    return Path(os.environ.get("USAGE_LOG_PATH",
                               str(_state_dir() / "usage.jsonl")))


def record(pipeline: str, model: str, usage: object) -> None:
    """Append one call's usage. `usage` is the SDK's response.usage object
    (or any object/dict with the four token fields; missing fields are 0)."""
    def field(name: str) -> int:
        if isinstance(usage, dict):
            v = usage.get(name, 0)
        else:
            v = getattr(usage, name, 0)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    row = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pipeline": pipeline,
        "model": model,
        "input_tokens": field("input_tokens"),
        "output_tokens": field("output_tokens"),
        "cache_creation_input_tokens": field("cache_creation_input_tokens"),
        "cache_read_input_tokens": field("cache_read_input_tokens"),
    }
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass


# List prices per million tokens (USD). Estimates only — see module docstring.
# Keyed on substrings so dated model ids and gateway aliases still match.
PRICES_PER_MTOK = [
    # (substring, input, output, cache_write, cache_read)
    ("haiku", 1.00, 5.00, 1.25, 0.10),
    ("sonnet", 3.00, 15.00, 3.75, 0.30),
    ("opus", 15.00, 75.00, 18.75, 1.50),
]
_DEFAULT_PRICE = (3.00, 15.00, 3.75, 0.30)  # price unknown: assume sonnet-class


def estimate_usd(row: dict) -> float:
    model = (row.get("model") or "").lower()
    for sub, pi, po, pw, pr in PRICES_PER_MTOK:
        if sub in model:
            prices = (pi, po, pw, pr)
            break
    else:
        prices = _DEFAULT_PRICE
    pi, po, pw, pr = prices
    return (row.get("input_tokens", 0) * pi
            + row.get("output_tokens", 0) * po
            + row.get("cache_creation_input_tokens", 0) * pw
            + row.get("cache_read_input_tokens", 0) * pr) / 1_000_000


def summarize(days: int = 7) -> list[dict]:
    """Aggregate the last `days` of usage per (pipeline, model). Returns rows
    sorted by estimated cost, highest first. Unreadable lines are skipped."""
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=days))
    buckets: dict[tuple[str, str], dict] = {}
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
            ts = _dt.datetime.fromisoformat(row["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts < cutoff:
            continue
        key = (row.get("pipeline", "?"), row.get("model", "?"))
        b = buckets.setdefault(key, {
            "pipeline": key[0], "model": key[1], "calls": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "est_usd": 0.0,
        })
        b["calls"] += 1
        for f in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            b[f] += int(row.get(f, 0) or 0)
        b["est_usd"] += estimate_usd(row)
    return sorted(buckets.values(), key=lambda b: -b["est_usd"])


# ---------- Claude Code transcript reader --------------------------------------
# Separate data source from the pipeline log above: Claude Code writes local
# JSONL transcripts for every session, and those carry per-response usage.
# This reads THIS MACHINE's transcripts only — sessions on other devices and
# claude.ai chat are invisible here, and on a seat plan the dollar figure is
# a list-price estimate, not a bill.

def transcripts_dir() -> Path:
    return Path(os.environ.get("CLAUDE_TRANSCRIPTS_DIR",
                               str(Path.home() / ".claude" / "projects")))


def summarize_claude_code(days: int = 30) -> dict | None:
    """Aggregate token usage from local Claude Code transcripts.

    Implementation notes that matter:
      - Resumed/forked sessions copy earlier messages into the new file, so
        entries are deduplicated on (message.id, requestId) — without this,
        every resume double-counts history.
      - Only files modified inside the window are opened, and lines are
        substring-prefiltered before json.loads: the 30-day corpus can run
        to tens of MB.
    Returns None when no transcripts exist. Never raises.
    """
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    root = transcripts_dir()
    if not root.is_dir():
        return None
    seen: set = set()
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    by_model: dict[str, dict] = {}
    calls = 0
    sessions: set = set()
    try:
        files = [p for p in root.rglob("*.jsonl")
                 if p.stat().st_mtime >= cutoff.timestamp()]
    except OSError:
        return None
    for path in files:
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("type") != "assistant":
                    continue
                try:
                    ts = _dt.datetime.fromisoformat(
                        str(j.get("timestamp", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                if ts < cutoff:
                    continue
                m = j.get("message") or {}
                usage = m.get("usage") or {}
                if not usage:
                    continue
                if not any(int(usage.get(f, 0) or 0) for f in total):
                    continue  # synthetic/zero-token placeholder entries
                key = (m.get("id"), j.get("requestId"))
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                calls += 1
                sessions.add(path.stem)
                model = m.get("model") or "?"
                b = by_model.setdefault(model, {
                    "model": model, "calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0, "est_usd": 0.0})
                b["calls"] += 1
                row = {"model": model}
                for f in total:
                    v = int(usage.get(f, 0) or 0)
                    total[f] += v
                    b[f] += v
                    row[f] = v
                b["est_usd"] += estimate_usd(row)
    if calls == 0:
        return None
    out = {"days": days, "calls": calls, "sessions": len(sessions),
           "est_usd": sum(b["est_usd"] for b in by_model.values()),
           "by_model": sorted(by_model.values(), key=lambda b: -b["est_usd"])}
    out.update(total)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Show recent LLM usage")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--claude-code", action="store_true",
                    help="summarize local Claude Code transcripts instead of "
                         "the pipeline log (default window 30 days)")
    args = ap.parse_args()
    if args.claude_code:
        cc = summarize_claude_code(args.days if args.days != 7 else 30)
        if not cc:
            print(f"No Claude Code transcripts found under {transcripts_dir()}.")
        else:
            print(f"Claude Code, last {cc['days']} days: {cc['sessions']} sessions, "
                  f"{cc['calls']} responses, ~${cc['est_usd']:.2f} at list price")
            for b in cc["by_model"]:
                toks = (b["input_tokens"] + b["output_tokens"]
                        + b["cache_creation_input_tokens"]
                        + b["cache_read_input_tokens"])
                print(f"  {b['model']:<32} {toks:>14,} tok  ~${b['est_usd']:.2f}")
        raise SystemExit(0)
    rows = summarize(args.days)
    if not rows:
        print(f"No usage recorded in the last {args.days} days "
              f"({log_path()}).")
    for b in rows:
        print(f"{b['pipeline']:<20} {b['model']:<30} calls={b['calls']:<5} "
              f"in={b['input_tokens']:<8} out={b['output_tokens']:<7} "
              f"cache_w={b['cache_creation_input_tokens']:<8} "
              f"cache_r={b['cache_read_input_tokens']:<8} "
              f"~${b['est_usd']:.2f}")
