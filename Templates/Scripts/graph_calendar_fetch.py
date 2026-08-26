#!/usr/bin/env python3
"""
graph_calendar_fetch.py — token-free-of-LLM PRODUCER for meeting pre-population.

Fetches today's calendar straight from Microsoft Graph and feeds the existing
deterministic transform (mcp_meeting_transform.py), which writes the schema-v1
handoff trio into the drop folder. Functionally equivalent to the Claude-CLI
producer path in meeting_pull.py, with three differences that matter:

  - Zero LLM tokens: the "producer" was always deterministic; the Claude
    session only existed to reach the M365 MCP connector. A direct Graph
    call removes the model from the loop entirely.
  - A ~2-second HTTP call fits inside any laptop wake window — the
    sleep-mid-session failure class disappears.
  - No dependency on Claude CLI auth state or MCP tool-name drift.

Auth: OAuth2 device-code flow against login.microsoftonline.com, delegated
scope Calendars.Read. One interactive sign-in seeds a refresh token, stored
via secret_store (macOS Keychain / Windows DPAPI); every later run refreshes
silently and re-stores the rotated token. The client id defaults to
Microsoft's pre-registered public "Microsoft Graph Command Line Tools" app,
overridable in config for tenants that require a first-party registration
(key "graph_client_id"); tenant authority defaults to "organizations"
(key "graph_auth_tenant" to pin).

Setup (one-time, interactively, at the desk):
    python3 graph_calendar_fetch.py --auth

Then flip the producer in .config/meeting_pull.json:
    "producer": "graph"
and meeting_pull.py (the 05:00 LaunchAgent / scheduled task) uses this
instead of the Claude CLI. All of meeting_pull's retry/skip-if-fresh/notify
plumbing applies unchanged.

Stdlib-only, Python 3.9-compatible (runs under /usr/bin/python3, like
meeting_pull.py).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Microsoft's public "Microsoft Graph Command Line Tools" client id — a
# well-known first-party public client that permits device-code sign-in in
# most tenants. Not a secret (public clients have none).
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
DEFAULT_AUTH_TENANT = "organizations"
SCOPE = "offline_access Calendars.Read"
TOKEN_SECRET = "GRAPH_REFRESH_TOKEN"

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT_FIELDS = ",".join([
    "id", "subject", "start", "end", "attendees", "organizer", "isAllDay",
    "bodyPreview", "sensitivity", "isCancelled", "categories", "location",
    "recurrence", "createdDateTime", "lastModifiedDateTime",
])


def log(message):
    print("[graph-fetch] %s" % message, flush=True)


def die(message):
    print("[graph-fetch] ERROR: %s" % message, file=sys.stderr, flush=True)
    raise SystemExit(1)


def load_config(path):
    if not path.is_file():
        die("config not found: %s (run: ./install.sh --only 54-meeting-pull)" % path)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die("config %s is not valid JSON: %s" % (path, exc))


def _form_post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read())
        except Exception:
            return None, {"error": "http_%d" % e.code}


def _authority(config):
    tenant = (config.get("graph_auth_tenant") or DEFAULT_AUTH_TENANT).strip()
    return "https://login.microsoftonline.com/%s/oauth2/v2.0" % tenant


def _client_id(config):
    return (config.get("graph_client_id") or DEFAULT_CLIENT_ID).strip()


def device_code_auth(config):
    """Interactive one-time sign-in; stores the refresh token in the keystore."""
    from secret_store import set_secret
    authority, client_id = _authority(config), _client_id(config)
    dc, err = _form_post(authority + "/devicecode",
                         {"client_id": client_id, "scope": SCOPE})
    if err:
        die("device-code request failed: %s — if your tenant blocks the "
            "public Graph CLI client, register an app and set "
            "graph_client_id in the config" % err.get("error"))
    print()
    print(dc.get("message") or
          "Visit %s and enter code %s" % (dc.get("verification_uri"),
                                          dc.get("user_code")))
    print()
    interval = int(dc.get("interval", 5))
    deadline = time.time() + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        tok, err = _form_post(authority + "/token", {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dc["device_code"]})
        if tok:
            refresh = tok.get("refresh_token")
            if not refresh:
                die("token response had no refresh_token — is offline_access "
                    "being stripped by tenant policy?")
            if not set_secret(TOKEN_SECRET, refresh):
                die("could not store the refresh token in the keystore")
            log("signed in; refresh token stored as %s" % TOKEN_SECRET)
            return 0
        code = (err or {}).get("error", "")
        if code == "authorization_pending":
            continue
        if code == "slow_down":
            interval += 5
            continue
        die("sign-in failed: %s" % code)
    die("device-code sign-in timed out")


def get_access_token(config):
    """Silent refresh. Rotates the stored refresh token when Azure returns a
    new one (it usually does)."""
    from secret_store import get_secret, set_secret
    refresh = get_secret(TOKEN_SECRET)
    if not refresh:
        die("no stored Graph refresh token — run: python3 %s --auth"
            % Path(__file__).name)
    tok, err = _form_post(_authority(config) + "/token", {
        "client_id": _client_id(config),
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": SCOPE})
    if err:
        code = err.get("error", "?")
        if code in ("invalid_grant", "interaction_required"):
            die("refresh token expired/revoked (%s) — run: python3 %s --auth"
                % (code, Path(__file__).name))
        die("token refresh failed: %s" % code)
    new_refresh = tok.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        set_secret(TOKEN_SECRET, new_refresh)
    return tok["access_token"]


def _graph_get(url, access_token):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + access_token,
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def fetch_events(access_token, day_start_iso, day_end_iso):
    """calendarView for the window, following pagination. Returns raw Graph
    event dicts."""
    params = urllib.parse.urlencode({
        "startDateTime": day_start_iso,
        "endDateTime": day_end_iso,
        "$select": SELECT_FIELDS,
        "$orderby": "start/dateTime",
        "$top": "100"})
    url = GRAPH + "/me/calendarView?" + params
    events = []
    while url:
        page = _graph_get(url, access_token)
        events.extend(page.get("value") or [])
        url = page.get("@odata.nextLink")
    return events


def flatten_event(ev):
    """Reshape a native Graph event into the flattened attendee/organizer
    shape mcp_meeting_transform.py consumes (the M365 MCP connector's
    read_resource shape). Everything else passes through under the same
    field names Graph already uses."""
    def flat_addr(entry):
        email_obj = (entry or {}).get("emailAddress") or {}
        return {"address": email_obj.get("address") or "",
                "name": email_obj.get("name") or ""}

    out = dict(ev)
    out["attendees"] = []
    for a in ev.get("attendees") or []:
        fa = flat_addr(a)
        fa["type"] = a.get("type") or "required"
        fa["responseStatus"] = ((a.get("status") or {}).get("response")
                                or "none")
        out["attendees"].append(fa)
    out["organizer"] = flat_addr(ev.get("organizer"))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", default=os.environ.get("MEETING_PULL_CONFIG"),
                        help="path to meeting_pull.json (default: .config/meeting_pull.json)")
    parser.add_argument("--out-dir", default=None,
                        help="drop folder for the handoff trio (default: config out_dir)")
    parser.add_argument("--date", default=None,
                        help="YYYY-MM-DD of the FIRST day to fetch (default: today in the "
                             "config timezone); the window extends lookahead_days beyond it")
    parser.add_argument("--auth", action="store_true",
                        help="interactive one-time device-code sign-in; stores the refresh token")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print the raw-events JSON; do not run the transform")
    args = parser.parse_args()

    config_path = (Path(args.config).expanduser() if args.config
                   else SCRIPTS_DIR / ".config" / "meeting_pull.json")
    config = load_config(config_path)

    if args.auth:
        return device_code_auth(config)

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(config.get("timezone") or "UTC")
    except Exception:
        die("config timezone %r is not a valid IANA zone"
            % config.get("timezone"))

    if args.date:
        day = _dt.date.fromisoformat(args.date)
    else:
        day = _dt.datetime.now(tz).date()

    # The window comes from meeting_pull.window_days() rather than being
    # recomputed here: both producers feed the same transform and the same
    # consumer, so any difference would make the vault's contents depend on
    # which producer ran. meeting_pull imports cleanly (module level is
    # constants and defs only).
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from meeting_pull import window_days
    try:
        day, last_day = window_days(config, first=day)
    except ValueError as exc:
        die(str(exc))

    day_start = _dt.datetime.combine(day, _dt.time.min, tzinfo=tz)
    day_end = _dt.datetime.combine(last_day + _dt.timedelta(days=1),
                                   _dt.time.min, tzinfo=tz)

    access_token = get_access_token(config)
    events = fetch_events(access_token,
                          day_start.isoformat(), day_end.isoformat())
    log("fetched %d event(s) for %s..%s" % (len(events), day.isoformat(),
                                            last_day.isoformat()))

    raw = {
        "user": {"display_name": config.get("display_name"),
                 "email": config.get("email"),
                 "tenant": config.get("tenant"),
                 "timezone": config.get("timezone")},
        "week": {"start": day.isoformat(), "end": last_day.isoformat()},
        "events": [flatten_event(e) for e in events],
    }

    if args.dry_run:
        print(json.dumps(raw, indent=2))
        return 0

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else Path(
        str(config.get("out_dir") or "")).expanduser()
    if not str(out_dir):
        die("no drop folder: pass --out-dir or set out_dir in the config")
    out_dir.mkdir(parents=True, exist_ok=True)

    domains = config.get("tenant_domains") or []
    if isinstance(domains, str):
        domains = [d.strip() for d in domains.split(",") if d.strip()]

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(raw, fh)
        tmp = fh.name
    try:
        cmd = [sys.executable, str(SCRIPTS_DIR / "mcp_meeting_transform.py"),
               "--input", tmp, "--out-dir", str(out_dir),
               "--run-date", day.isoformat(),
               "--tenant-domains", ",".join(domains) or "example.edu"]
        completed = subprocess.run(cmd)
        if completed.returncode != 0:
            die("transform exited %d" % completed.returncode)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    log("handoff written to %s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
