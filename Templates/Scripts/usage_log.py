#!/usr/bin/env python3
"""usage_log.py — per-pipeline LLM token accounting.

Every script that calls a hosted model appends one JSON line per API call:

    {"ts": ..., "pipeline": "tag_clippings", "model": "...",
     "input_tokens": N, "output_tokens": N,
     "cache_creation_input_tokens": N, "cache_read_input_tokens": N,
     "cache_creation_5m_input_tokens": N, "cache_creation_1h_input_tokens": N}

The two trailing fields break the cache-write total down by TTL, which is what
it is priced on. They are a breakdown OF cache_creation_input_tokens, not an
addition to it — never sum the token fields to get a total; use total_tokens().

The morning dashboard reads the last N days and renders tokens per pipeline
plus a dollar figure estimated from Anthropic list prices. The estimate is
labeled as such — an install running through a gateway (Bedrock, LiteLLM,
an institutional proxy) pays whatever its contract says, but relative cost
per pipeline is still the number that tells you where to optimize.

Two token totals, and the difference matters. billable_tokens() counts fresh
input + output + cache writes; total_tokens() adds cache reads. On an agentic
workload the reads run 95%+ of the raw total, because every turn re-reads the
whole accumulated context — so the raw total tracks how long a session ran,
not how much was accomplished, and it overstates what a metered bill would be
by a wide margin (reads are priced at 0.1x input). Report billable; keep the
raw total as a secondary throughput line.

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


# Every per-call token field carried through the log and the summaries. The
# two TTL fields are a breakdown OF cache_creation_input_tokens, not additions
# to it — never sum this tuple to get a token total; use total_tokens().
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens",
)


def record(pipeline: str, model: str, usage: object) -> None:
    """Append one call's usage. `usage` is the SDK's response.usage object
    (or any object/dict with the four token fields; missing fields are 0)."""
    def _int(v: object) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def field(name: str) -> int:
        if isinstance(usage, dict):
            return _int(usage.get(name, 0))
        return _int(getattr(usage, name, 0))

    def write_ttl(name: str) -> int:
        """Cache writes are priced by TTL, and the split lives one level down
        in usage.cache_creation. Absent on older SDKs — 0 then, and
        estimate_usd() falls back to the flat total."""
        if isinstance(usage, dict):
            cc = usage.get("cache_creation")
        else:
            cc = getattr(usage, "cache_creation", None)
        if cc is None:
            return 0
        if isinstance(cc, dict):
            return _int(cc.get(name, 0))
        return _int(getattr(cc, name, 0))

    row = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pipeline": pipeline,
        "model": model,
        "input_tokens": field("input_tokens"),
        "output_tokens": field("output_tokens"),
        "cache_creation_input_tokens": field("cache_creation_input_tokens"),
        "cache_read_input_tokens": field("cache_read_input_tokens"),
        "cache_creation_5m_input_tokens": write_ttl("ephemeral_5m_input_tokens"),
        "cache_creation_1h_input_tokens": write_ttl("ephemeral_1h_input_tokens"),
    }
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass


# List prices per million tokens (USD), current as of 2026-08-25. Estimates
# only — see module docstring. Keyed on substrings so dated model ids and
# gateway aliases still match. ORDER MATTERS: first match wins, so specific
# entries sit above the family ones (claude-3-opus before opus).
PRICES_PER_MTOK = [
    # (substring, input, output)
    ("claude-3-opus", 15.00, 75.00),  # legacy Opus 3 — must precede "opus"
    ("mythos", 10.00, 50.00),
    ("fable", 10.00, 50.00),
    ("opus", 5.00, 25.00),            # Opus 5 / 4.8 / 4.7 / 4.6
    ("sonnet", 3.00, 15.00),          # Sonnet 5 ran a $2/$10 introductory
                                      # rate through 2026-08-31; the standing
                                      # rate is used here, so estimates over
                                      # that window run slightly high
    ("haiku", 1.00, 5.00),
]
_DEFAULT_PRICE = (3.00, 15.00)  # price unknown: assume sonnet-class

# Cache tokens are NOT free, and they are not a separate price column: both are
# multiples of the model's own input price.
#   read  = 0.10x input
#   write = 1.25x input at the 5-minute TTL, 2.00x at the 1-hour TTL
# Claude Code writes at the 1-hour TTL, so pricing every write at the 5-minute
# multiple understates the write line by ~60%.
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT_5M = 1.25
CACHE_WRITE_MULT_1H = 2.00


def _prices(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for sub, pi, po in PRICES_PER_MTOK:
        if sub in m:
            return pi, po
    return _DEFAULT_PRICE


def estimate_usd(row: dict) -> float:
    pi, po = _prices(row.get("model") or "")
    w5 = int(row.get("cache_creation_5m_input_tokens", 0) or 0)
    w1 = int(row.get("cache_creation_1h_input_tokens", 0) or 0)
    # Reconcile against the flat total rather than testing the split for
    # absence: rows written before TTL capture carry a flat total with both
    # TTL fields zero, and testing `is None` would price those writes at zero.
    # Any unattributed remainder goes to the 1-hour multiple — what Claude Code
    # uses, and overstating a cost estimate is the safe direction.
    unsplit = int(row.get("cache_creation_input_tokens", 0) or 0) - w5 - w1
    if unsplit > 0:
        w1 += unsplit
    return (int(row.get("input_tokens", 0) or 0) * pi
            + int(row.get("output_tokens", 0) or 0) * po
            + int(w5 or 0) * pi * CACHE_WRITE_MULT_5M
            + int(w1 or 0) * pi * CACHE_WRITE_MULT_1H
            + int(row.get("cache_read_input_tokens", 0) or 0) * pi
            * CACHE_READ_MULT) / 1_000_000


def cache_read_usd(row: dict) -> float:
    """The cache-read slice of estimate_usd(), so callers can show what share
    of the bill is re-read context rather than new work."""
    pi, _ = _prices(row.get("model") or "")
    return (int(row.get("cache_read_input_tokens", 0) or 0) * pi
            * CACHE_READ_MULT) / 1_000_000


def billable_tokens(row: dict) -> int:
    """Fresh input + output + cache writes. EXCLUDES cache reads.

    Cache reads swamp any raw token total — 95%+ of it on a long agentic
    session — because every turn re-reads the whole accumulated context. That
    makes the grand total a measure of context throughput, not of work done:
    it grows with session length even when nothing new is accomplished. This
    is the figure to track week over week."""
    return (int(row.get("input_tokens", 0) or 0)
            + int(row.get("output_tokens", 0) or 0)
            + int(row.get("cache_creation_input_tokens", 0) or 0))


def total_tokens(row: dict) -> int:
    """Every token that crossed the wire, cache reads included — context
    throughput. Real, but not proportional to effort; see billable_tokens()."""
    return (billable_tokens(row)
            + int(row.get("cache_read_input_tokens", 0) or 0))


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
            **{f: 0 for f in _TOKEN_FIELDS}, "est_usd": 0.0,
        })
        b["calls"] += 1
        for f in _TOKEN_FIELDS:
            b[f] += int(row.get(f, 0) or 0)
        b["est_usd"] += estimate_usd(row)
    for b in buckets.values():
        b["billable_tokens"] = billable_tokens(b)
        b["total_tokens"] = total_tokens(b)
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
    total = {f: 0 for f in _TOKEN_FIELDS}
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
                # The cache-write TTL split sits one level down; flatten it so
                # estimate_usd() can price 5m and 1h writes at their own
                # multiples instead of guessing.
                cc = usage.get("cache_creation")
                if isinstance(cc, dict):
                    usage = dict(usage)
                    usage["cache_creation_5m_input_tokens"] = cc.get(
                        "ephemeral_5m_input_tokens", 0)
                    usage["cache_creation_1h_input_tokens"] = cc.get(
                        "ephemeral_1h_input_tokens", 0)
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
                    "model": model, "calls": 0,
                    **{f: 0 for f in _TOKEN_FIELDS}, "est_usd": 0.0,
                    "cache_read_usd": 0.0})
                b["calls"] += 1
                row = {"model": model}
                for f in total:
                    v = int(usage.get(f, 0) or 0)
                    total[f] += v
                    b[f] += v
                    row[f] = v
                b["est_usd"] += estimate_usd(row)
                b["cache_read_usd"] += cache_read_usd(row)
    if calls == 0:
        return None
    for b in by_model.values():
        b["billable_tokens"] = billable_tokens(b)
        b["total_tokens"] = total_tokens(b)
    out = {"days": days, "calls": calls, "sessions": len(sessions),
           "est_usd": sum(b["est_usd"] for b in by_model.values()),
           "cache_read_usd": sum(b["cache_read_usd"]
                                 for b in by_model.values()),
           "by_model": sorted(by_model.values(), key=lambda b: -b["est_usd"])}
    out.update(total)
    out["billable_tokens"] = billable_tokens(out)
    out["total_tokens"] = total_tokens(out)
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
            read_pct = (100 * cc["cache_read_usd"] / cc["est_usd"]
                        if cc["est_usd"] else 0.0)
            print(f"Claude Code, last {cc['days']} days: {cc['sessions']} sessions, "
                  f"{cc['calls']} responses, ~${cc['est_usd']:.2f} at list price")
            print(f"  billable (in+out+cache writes): "
                  f"{cc['billable_tokens']:>14,} tok")
            print(f"  context throughput (incl. reads): "
                  f"{cc['total_tokens']:>14,} tok  "
                  f"({cc['cache_read_input_tokens'] / cc['total_tokens']:.1%} "
                  f"cache re-reads, {read_pct:.0f}% of the estimate)")
            for b in cc["by_model"]:
                print(f"  {b['model']:<32} {b['billable_tokens']:>12,} billable"
                      f" / {b['total_tokens']:>14,} tok  ~${b['est_usd']:.2f}")
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
              f"billable={b['billable_tokens']:<9} "
              f"~${b['est_usd']:.2f}")
