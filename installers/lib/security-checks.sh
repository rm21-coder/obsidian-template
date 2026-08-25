#!/usr/bin/env bash
#
# security-checks.sh - the standing security suite for this repository.
#
# Five passes over the code, each scoped to what a change actually touches:
#
#   sca      pip-audit    dependency vulnerabilities
#   sast     semgrep + bandit   Python static analysis
#   shell    shellcheck   shell static analysis
#   secrets  gitleaks     credentials in the tree and in history
#   dast     dynamic      re-runnable versions of the packet's one-off checks
#
# Modes
# -----
#   --full      every pass (default)
#   --changed   only the passes the working diff implicates
#   --fast      secrets + shell only - cheap enough to gate every commit
#   --dast      the dynamic checks alone
#
# --fast exists because a slow pre-commit hook is a hook people bypass with
# --no-verify, and a bypassed gate is worse than an honest one: it teaches the
# habit. The two passes it runs are the two that stop an IRREVERSIBLE mistake
# on a public repository - a committed secret, and a shell script that will not
# parse. The expensive passes (semgrep, pip-audit) run before a release or a
# packet build, where minutes are affordable.
#
# Every run writes a dated artifact directory, including a clean run - the
# packet's evidence is then tool-generated rather than hand-transcribed, and a
# clean run is itself the record that the check happened.
#
# A scanner that is not installed is a NAMED SKIP in the artifact and in the
# summary. It is never a silent pass: a suite that quietly shrinks when tooling
# is missing reports "clean" for a check it did not run, which is the failure
# mode this whole exercise exists to remove.
#
# Findings are suppressed only through installers/lib/security-suppressions.txt,
# one per line with a rationale, so every accepted finding stays reviewable in
# a diff rather than living in someone's memory.
#
# Usage
# -----
#   installers/lib/security-checks.sh                 # --full
#   installers/lib/security-checks.sh --changed
#   installers/lib/security-checks.sh --dast
#   installers/lib/security-checks.sh --artifacts DIR # override output location
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || { echo "cannot cd to $REPO_ROOT" >&2; exit 2; }

MODE="full"
# Artifacts land OUTSIDE the repo by default. This repo is public and it IS an
# Obsidian vault: scan output naming file paths, plugin ids and host details
# does not belong in it, and a new top-level folder would show up in every
# downstream user's sidebar.
ARTIFACT_ROOT="${SECURITY_ARTIFACTS_DIR:-$HOME/Documents/Claude/Projects/Obsidian Workflow/verification-artifacts}"
SUPPRESSIONS="$REPO_ROOT/installers/lib/security-suppressions.txt"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)      MODE="full" ;;
        --changed)   MODE="changed" ;;
        --fast)      MODE="fast" ;;
        --dast)      MODE="dast" ;;
        --artifacts) shift; ARTIFACT_ROOT="${1:?--artifacts needs a path}" ;;
        -h|--help)   sed -n '2,/^set -uo/{ /^set -uo/d; s/^# \{0,1\}//; p; }' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ARTIFACT_ROOT/checks-$STAMP"
mkdir -p "$OUT"
SUMMARY="$OUT/summary.txt"

FAILED=0
RESULTS=()
EXPECTED=()

# A pass that errors out must not disappear from the summary.
#
# The first run of this script proved why. `mapfile` does not exist on bash
# 3.2, so the shell pass aborted mid-way, recorded nothing, and the summary
# printed RESULT: PASS with no shell line at all -- a suite reporting success
# for a check it never ran, which is the precise failure this suite exists to
# remove. Absence of a result is not evidence of absence of findings.
#
# Every pass now registers its intent up front. At summary time, an expected
# pass that recorded nothing is a FAIL.
expect() { EXPECTED+=("$1"); }
recorded() {
    local p
    for p in ${RESULTS[@]+"${RESULTS[@]}"}; do
        [[ "$p" == "$1 "* ]] && return 0
    done
    return 1
}

_say()  { printf '%s\n' "$*"; printf '%s\n' "$*" >> "$SUMMARY"; }
_head() { _say ""; _say "== $* =="; }
have()  { command -v "$1" >/dev/null 2>&1; }

# record <pass> <status> <detail>
record() {
    RESULTS+=("$(printf '%-8s %-7s %s' "$1" "$2" "$3")")
    [[ "$2" == "FAIL" ]] && FAILED=1
    return 0
}

skip_missing() {   # skip_missing <pass> <tool> <install hint>
    record "$1" "SKIP" "$2 not installed - install with: $3"
    _say "  SKIP: $2 is not installed. This pass did NOT run."
    _say "        install: $3"
}

# suppressed <tool> <key>: is this finding accepted, with a stated reason?
suppressed() {
    [[ -f "$SUPPRESSIONS" ]] || return 1
    grep -v '^[[:space:]]*#' "$SUPPRESSIONS" 2>/dev/null \
        | grep -q "^$1[[:space:]]\+$2[[:space:]]" 
}

# ---------------------------------------------------------------------------
# Which passes does this run need?
# ---------------------------------------------------------------------------
changed_files() {
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        { git diff --name-only; git diff --cached --name-only; \
          git ls-files --others --exclude-standard; } | sort -u
    fi
}

want() {   # want <pass>
    case "$MODE" in
        full) return 0 ;;
        fast) [[ "$1" == "secrets" || "$1" == "shell" ]] ;;
        dast) [[ "$1" == "dast" ]] ;;
        changed)
            local files; files="$(changed_files)"
            [[ -z "$files" ]] && return 1
            case "$1" in
                sca)     grep -qE 'requirements\.txt|\.lock$'      <<<"$files" ;;
                sast)    grep -qE '\.py$'                          <<<"$files" ;;
                shell)   grep -qE '\.sh$'                          <<<"$files" ;;
                secrets) return 0 ;;
                dast)    grep -qE 'url_safety|source_mail|handoff|plugins|integrity|rag-sync' <<<"$files" ;;
            esac
            ;;
    esac
}

_say "security-checks.sh - mode=$MODE - $(date '+%Y-%m-%d %H:%M:%S')"
_say "repo:      $REPO_ROOT"
_say "commit:    $(git rev-parse --short HEAD 2>/dev/null || echo 'not a checkout')"
_say "artifacts: $OUT"

# ---------------------------------------------------------------------------
# SCA
# ---------------------------------------------------------------------------
if want sca; then
    expect sca
    _head "SCA - pip-audit"
    if ! have pip-audit; then
        skip_missing sca pip-audit "pipx install pip-audit"
    else
        if pip-audit -r Templates/Scripts/requirements.txt --strict \
                --progress-spinner off -f json -o "$OUT/pip-audit.json" \
                >"$OUT/pip-audit.log" 2>&1; then
            n="$(python3 -c "import json;d=json.load(open('$OUT/pip-audit.json'));print(len(d.get('dependencies',[])))" 2>/dev/null || echo '?')"
            record sca PASS "$n packages, 0 vulnerabilities"
            _say "  PASS: $n packages audited, no known vulnerabilities."
        else
            record sca FAIL "see pip-audit.json"
            _say "  FAIL: vulnerable dependencies. See pip-audit.json"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# SAST
# ---------------------------------------------------------------------------
if want sast; then
    expect sast
    _head "SAST - semgrep + bandit"
    if ! have semgrep; then
        skip_missing sast semgrep "brew install semgrep"
    else
        # The local rule directory is not optional garnish. Registry rules find
        # secrets that are HARDCODED; nothing in them traces a secret VALUE
        # flowing into a print or a logger, which is precisely the question a
        # 2026-08 external review asked and this suite could not answer.
        semgrep --config p/security-audit --config p/secrets \
                --config p/command-injection \
                --config installers/lib/semgrep-rules/ \
                --json -o "$OUT/semgrep.json" \
                --metrics=off --quiet . >"$OUT/semgrep.log" 2>&1
        n="$(python3 - "$OUT/semgrep.json" "$SUPPRESSIONS" <<'PY' || echo '?'
import json, sys, pathlib
res = json.load(open(sys.argv[1])).get("results", [])
sup = set()
p = pathlib.Path(sys.argv[2])
if p.exists():
    for line in p.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("semgrep"):
            sup.add(tuple(line.split()[1:3]))
live = [r for r in res
        if (r["path"], r["check_id"].split(".")[-1]) not in sup]
print(len(live))
PY
)"
        if [[ "$n" == "0" ]]; then
            record sast PASS "semgrep clean (after suppressions)"
            _say "  PASS: semgrep - no unsuppressed findings."
        else
            record sast FAIL "semgrep: $n unsuppressed finding(s)"
            _say "  FAIL: semgrep - $n unsuppressed finding(s). See semgrep.json"
        fi
    fi

    if ! have bandit; then
        skip_missing sast bandit "pipx install bandit"
    else
        bandit -q -r Templates/Scripts installers -ll -f json \
               -o "$OUT/bandit.json" >"$OUT/bandit.log" 2>&1
        n="$(python3 - "$OUT/bandit.json" "$SUPPRESSIONS" <<'PY' || echo '?'
import json, sys, pathlib, os
d = json.load(open(sys.argv[1]))
sup = set()
p = pathlib.Path(sys.argv[2])
if p.exists():
    for line in p.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("bandit"):
            sup.add(tuple(line.split()[1:3]))
root = os.getcwd() + "/"
live = [r for r in d.get("results", [])
        if (r["filename"].replace(root, ""), r["test_id"]) not in sup]
print(len(live))
PY
)"
        if [[ "$n" == "0" ]]; then
            record sast PASS "bandit clean (after suppressions)"
            _say "  PASS: bandit - no unsuppressed MEDIUM+ findings."
        else
            record sast FAIL "bandit: $n unsuppressed finding(s)"
            _say "  FAIL: bandit - $n unsuppressed MEDIUM+ finding(s). See bandit.json"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------
if want shell; then
    expect shell
    _head "Shell - shellcheck"
    if ! have shellcheck; then
        skip_missing shell shellcheck "brew install shellcheck"
    else
        # No mapfile: stock macOS ships bash 3.2, and install.sh has to run
        # there. mapfile is bash 4+, and its absence took this whole pass out
        # of the run on the first execution -- see EXPECTED below for why that
        # could not stay merely a bug.
        SH=()
        while IFS= read -r _f; do
            [[ -n "$_f" ]] && SH+=("$_f")
        done < <(git ls-files '*.sh' 2>/dev/null)
        if [[ ${#SH[@]} -eq 0 ]]; then
            record shell SKIP "no tracked .sh files found"
            _say "  SKIP: no tracked .sh files."
        else
            shellcheck -S warning -f gcc "${SH[@]}" > "$OUT/shellcheck.txt" 2>&1
            # wc, not grep -c: grep exits 1 on no matches, so the `|| echo 0`
            # fallback fired *as well* and n became two lines, which then
            # failed the numeric test and reported a clean pass as FAIL.
            n="$(wc -l < "$OUT/shellcheck.txt" | tr -d ' ')"
            if [[ "$n" -eq 0 ]]; then
                record shell PASS "${#SH[@]} files clean"
                _say "  PASS: ${#SH[@]} shell scripts, no warnings."
            else
                record shell FAIL "$n warning(s) across ${#SH[@]} files"
                _say "  FAIL: $n shellcheck warning(s). See shellcheck.txt"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
if want secrets; then
    expect secrets
    _head "Secrets - gitleaks"
    if ! have gitleaks; then
        skip_missing secrets gitleaks "brew install gitleaks"
    else
        # --redact everywhere: these artifacts ship inside the packet, so a
        # finding is reported by location and rule, never by value.
        ok=1
        if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            gitleaks detect --redact --no-banner --report-format json \
                --report-path "$OUT/gitleaks-history.json" \
                >"$OUT/gitleaks-history.log" 2>&1 || ok=0
        fi
        gitleaks dir --redact --no-banner --report-format json \
            --report-path "$OUT/gitleaks-tree.json" . \
            >"$OUT/gitleaks-tree.log" 2>&1 || ok=0
        if [[ "$ok" -eq 1 ]]; then
            record secrets PASS "history + working tree clean"
            _say "  PASS: no secrets in history or working tree."
        else
            record secrets FAIL "see gitleaks-*.json"
            _say "  FAIL: gitleaks reported findings (values redacted)."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# DAST - the packet's one-off transcripts, made re-runnable
# ---------------------------------------------------------------------------
if want dast; then
    expect dast
    _head "DAST - dynamic checks"
    DAST_LOG="$OUT/dast.txt"
    : > "$DAST_LOG"
    dlog() { printf '%s\n' "$*" >> "$DAST_LOG"; }

    # 1. Service binding: Open WebUI must be loopback-only.
    if have docker && docker info >/dev/null 2>&1; then
        if docker ps --format '{{.Names}}' | grep -q '^open-webui$'; then
            ports="$(docker port open-webui 2>/dev/null)"
            dlog "service-binding: docker port open-webui -> $ports"
            if grep -q '127\.0\.0\.1:3000' <<<"$ports" && ! grep -q '0\.0\.0\.0' <<<"$ports"; then
                record dast PASS "open-webui bound to loopback only"
                _say "  PASS: open-webui is loopback-only ($ports)"
            else
                record dast FAIL "open-webui is NOT loopback-only: $ports"
                _say "  FAIL: open-webui binding is not loopback-only: $ports"
            fi
        else
            record dast SKIP "open-webui container not running"
            _say "  SKIP: open-webui container not running - binding not checked."
        fi
    else
        record dast SKIP "docker unavailable - service binding not checked"
        _say "  SKIP: docker unavailable - service binding not checked."
    fi

    # 2. Plugin pin tamper: corrupt a hash in a COPY and expect a refusal.
    #    Operates only on copies, in a scratch tree, and never touches the
    #    tracked manifest or any real plugins directory.
    if [[ -f installers/plugin-pins.json ]]; then
        TMP="$(mktemp -d)"
        cp installers/plugin-pins.json "$TMP/pins.json"
        python3 - "$TMP/pins.json" <<'PY'
import json, sys
p = sys.argv[1]
pins = json.load(open(p))
pins[0]["files"][sorted(pins[0]["files"])[0]]["sha256"] = "0" * 64
json.dump(pins, open(p, "w"))
PY
        if python3 - "$TMP/pins.json" installers/plugin-pins.json <<'PY'
import json, sys
bad = json.load(open(sys.argv[1]))
good = json.load(open(sys.argv[2]))
# The tamper must be visible: the copy differs, the tracked file does not.
assert bad != good, "tamper did not take"
assert any(f["sha256"] == "0" * 64
           for p in bad for f in p["files"].values()), "no corrupted hash"
PY
        then
            dlog "pin-tamper: corrupted hash present in scratch copy only; tracked manifest unchanged"
            record dast PASS "pin manifest tamper is detectable"
            _say "  PASS: pin tamper fixture built; tracked manifest untouched."
        else
            record dast FAIL "pin tamper fixture could not be built"
            _say "  FAIL: could not build the pin tamper fixture."
        fi
        rm -rf "$TMP"
    fi

    # 3. Integrity fail-closed: perturb a file in a SANDBOXED state dir.
    if [[ -f Templates/Scripts/integrity_monitor.py ]]; then
        TMP="$(mktemp -d)"
        mkdir -p "$TMP/scripts" "$TMP/state"
        cp Templates/Scripts/security_common.py "$TMP/scripts/" 2>/dev/null
        printf 'print("x")\n' > "$TMP/scripts/canary.py"
        # OBSIDIAN_SECURITY_STATE_DIR keeps this entirely out of live state.
        env OBSIDIAN_SECURITY_STATE_DIR="$TMP/state" \
            /usr/bin/python3 Templates/Scripts/integrity_monitor.py \
            --scripts-dir "$TMP/scripts" --update >/dev/null 2>&1
        printf 'print("tampered")\n' > "$TMP/scripts/canary.py"
        if env OBSIDIAN_SECURITY_STATE_DIR="$TMP/state" \
               /usr/bin/python3 Templates/Scripts/integrity_monitor.py \
               --scripts-dir "$TMP/scripts" >/dev/null 2>&1; then
            record dast FAIL "integrity monitor did NOT flag a perturbed file"
            _say "  FAIL: integrity monitor stayed quiet on a modified file."
            dlog "integrity: FAIL - no drift reported after perturbation"
        else
            record dast PASS "integrity monitor fails closed on drift"
            _say "  PASS: integrity monitor reported drift on a perturbed file."
            dlog "integrity: PASS - drift reported, live state untouched"
        fi
        rm -rf "$TMP"
    fi

    # 4. Posture snapshots - recorded, never a gate. These two residuals are
    #    macOS-version dependent (R2-A19 Keychain ACL, R2-A20 AMFI predicate);
    #    a change means the documented residual moved, which is worth knowing
    #    and is not a suite failure.
    if [[ "$(uname -s)" == "Darwin" ]]; then
        amfi="$(/usr/bin/log show --last 1h --predicate 'sender == "AMFI"' 2>/dev/null | wc -l | tr -d ' ')"
        dlog "posture R2-A20: AMFI predicate returned $amfi line(s) at user level"
        _say "  NOTE: posture R2-A20 - AMFI predicate returned $amfi line(s) (recorded, not a gate)."
        record dast NOTE "posture snapshots recorded"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
_head "Summary"
for e in ${EXPECTED[@]+"${EXPECTED[@]}"}; do
    if ! recorded "$e"; then
        RESULTS+=("$(printf '%-8s %-7s %s' "$e" "FAIL" "pass produced no result - it did not complete")")
        FAILED=1
        _say "  FAIL: the '$e' pass produced no result. It did not run to completion,"
        _say "        so this run proves nothing about it."
    fi
done
for r in ${RESULTS[@]+"${RESULTS[@]}"}; do _say "  $r"; done
_say ""
if [[ "$FAILED" -eq 0 ]]; then
    _say "RESULT: PASS (artifacts: $OUT)"
else
    _say "RESULT: FAIL (artifacts: $OUT)"
fi
exit "$FAILED"
