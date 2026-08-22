#!/usr/bin/env python3
"""pin_plugins.py — (re)generate installers/plugin-pins.json.

The installers do NOT fetch "latest" plugin releases: whatever release is
current when a maintainer runs this tool gets pinned — repo, release tag
(or commit, for release-less plugins), download URL, and SHA256 per file —
and installs verify every byte against those hashes, failing closed. A
compromised or merely surprising upstream release cannot reach an adopter's
vault until a maintainer reruns this tool and commits the diff, which is
exactly the review moment the pin exists to create.

Usage (maintainer, with network):
    python3 installers/lib/pin_plugins.py            # refresh all pins
    python3 installers/lib/pin_plugins.py --check    # verify pins still fetch

Reads:  .obsidian/community-plugins.json  (the vault's enabled-plugin list)
Writes: installers/plugin-pins.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_URL = ("https://raw.githubusercontent.com/obsidianmd/"
                "obsidian-releases/master/community-plugins.json")
PINS_PATH = REPO_ROOT / "installers" / "plugin-pins.json"
FILES = ("manifest.json", "main.js", "styles.css")   # styles.css optional


def _gh_token() -> str:
    try:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _get(url: str, token: str = "") -> bytes:
    req = urllib.request.Request(url)
    if token and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="re-download pinned URLs and verify hashes")
    args = ap.parse_args()

    ids = json.loads(
        (REPO_ROOT / ".obsidian" / "community-plugins.json").read_text())
    token = _gh_token()

    if args.check:
        pins = json.loads(PINS_PATH.read_text())
        bad = 0
        for pin in pins:
            for fname, meta in pin["files"].items():
                data = _get(meta["url"], token)
                got = hashlib.sha256(data).hexdigest()
                status = "OK" if got == meta["sha256"] else "HASH MISMATCH"
                if got != meta["sha256"]:
                    bad += 1
                print(f"  {pin['id']}/{fname}: {status}")
        print(f"{'FAIL' if bad else 'PASS'}: {bad} mismatches")
        return 1 if bad else 0

    registry = {p["id"]: p["repo"]
                for p in json.loads(_get(REGISTRY_URL, token))}
    pins = []
    for pid in ids:
        repo = registry.get(pid)
        if not repo:
            print(f"  {pid}: NOT IN REGISTRY — skipped", file=sys.stderr)
            continue
        tag = ""
        try:
            rel = json.loads(_get(
                f"https://api.github.com/repos/{repo}/releases/latest", token))
            tag = rel.get("tag_name", "")
        except urllib.error.HTTPError:
            pass
        if tag:
            base = f"https://github.com/{repo}/releases/download/{tag}"
            ref_desc = {"kind": "release", "ref": tag}
        else:
            # No releases: pin raw files at the current HEAD commit, so the
            # bytes are still immutable.
            head = json.loads(_get(
                f"https://api.github.com/repos/{repo}/commits/HEAD", token))
            sha = head["sha"]
            base = f"https://raw.githubusercontent.com/{repo}/{sha}"
            ref_desc = {"kind": "commit", "ref": sha}
        files = {}
        for fname in FILES:
            try:
                data = _get(f"{base}/{fname}", token)
            except urllib.error.HTTPError:
                if fname == "styles.css":
                    continue          # optional
                print(f"  {pid}: missing required {fname} at {base} — "
                      f"skipped entirely", file=sys.stderr)
                files = None
                break
            files[fname] = {"url": f"{base}/{fname}",
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data)}
        if not files:
            continue
        pins.append({"id": pid, "repo": repo, **ref_desc, "files": files})
        print(f"  pinned {pid} @ {ref_desc['ref']} "
              f"({len(files)} files)")

    PINS_PATH.write_text(json.dumps(pins, indent=2) + "\n")
    print(f"\nwrote {PINS_PATH} ({len(pins)}/{len(ids)} plugins pinned)")
    return 0 if len(pins) == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
