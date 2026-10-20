#!/bin/bash
#
# run_tests.sh — entry point for the security-controls test suite.
#
# What this does:
#   1. Builds (once) a separate test venv, Templates/Scripts/.venv-test, and
#      installs the test dependencies into it ONLY from requirements-dev.lock:
#      every package at a locked release, every file hash-checked, through
#      the installers' own install_requirements (installers/lib/common.sh).
#      Never into the scheduled jobs' .venv, which stays exactly
#      requirements.lock, and never into the system Python. Until 2026-10-12
#      this step pip-installed pytest & co. unpinned and unhashed into
#      whatever python3 was first on PATH. What the suite needs, and why
#      (numpy, for one), is in requirements-dev.txt.
#   2. Runs the suite under pytest with coverage on the patched .py
#      files (integrity_monitor, plugin_integrity_check,
#      youtube_summarize).
#   3. Generates an HTML coverage report at coverage_html/ alongside.
#   4. Prints a final pass/fail + coverage summary.
#
# The base interpreter is $TEST_PYTHON, else the first Homebrew python3.13 /
# 3.12 / 3.14 / python3 found; the lock's own checks (Apple Silicon,
# Python 3.10-3.14, GIL build) refuse anything it has no wheels for. Delete
# .venv-test to rebuild it from scratch.
#
# Default behavior is fully mocked — no real Keychain reads, no real
# network, no real ~/.local/share/obsidian-security touch. See
# conftest.py for the isolation fixtures.
#
# Usage:
#   ./run_tests.sh           # full suite
#   ./run_tests.sh -v        # verbose
#   ./run_tests.sh -k name   # run a specific test by substring
#   ./run_tests.sh --no-cov  # skip coverage (faster iteration)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$(dirname "$HERE")"

cd "$HERE"

# ---------------------------------------------------------------------------
# 1. Dependency check.
# ---------------------------------------------------------------------------

REPO="$(cd "$SCRIPTS/../.." && pwd)"
VENV="$SCRIPTS/.venv-test"
VPY="$VENV/bin/python"
# shellcheck source=../../../installers/lib/common.sh
source "$REPO/installers/lib/common.sh"

if [ ! -x "$VPY" ]; then
    base="${TEST_PYTHON:-}"
    if [ -z "$base" ]; then
        for cand in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                    /opt/homebrew/bin/python3.14 "$(command -v python3 || true)"; do
            if [ -n "$cand" ] && [ -x "$cand" ]; then base="$cand"; break; fi
        done
    fi
    if [ -z "$base" ]; then
        echo "[tests] FATAL: no python3 found; set TEST_PYTHON" >&2
        exit 2
    fi
    echo "[tests] creating the test venv $VENV from $base"
    "$base" -m venv "$VENV"
fi
if ! install_requirements "$VPY" "$SCRIPTS/requirements-dev.txt" requirements-dev.lock; then
    echo "[tests] FATAL: could not install requirements-dev.lock into $VENV" >&2
    exit 2
fi

# Verify the runtime files under test are present (otherwise tests won't import).
for f in integrity_monitor.py plugin_integrity_check.py youtube_summarize.py; do
    if [ ! -f "$SCRIPTS/$f" ]; then
        echo "[tests] FATAL: missing $SCRIPTS/$f — is this running from a full checkout?" >&2
        exit 2
    fi
done

# ---------------------------------------------------------------------------
# 2. Argument handling.
# ---------------------------------------------------------------------------

cov_args=(
    "--cov=integrity_monitor"
    "--cov=plugin_integrity_check"
    "--cov=youtube_summarize"
    "--cov=templater_guard"
    "--cov=output_sanitizer"
    "--cov=classification_tier"
    "--cov=disclosure_check"
    "--cov=meeting_pull"
    "--cov=email_tasks"
    "--cov=email_task_transform"
    "--cov=security_common"
    "--cov-report=term-missing"
    "--cov-report=html:$HERE/coverage_html"
)

extra_args=()
for a in "$@"; do
    case "$a" in
        --no-cov) cov_args=() ;;
        *) extra_args+=("$a") ;;
    esac
done

# ---------------------------------------------------------------------------
# 3. Run.
# ---------------------------------------------------------------------------

echo "[tests] running suite from $HERE"
echo "[tests] under-test scripts: $SCRIPTS"
echo ""

# PYTHONPATH so pytest can import the modules under test for coverage.
export PYTHONPATH="$SCRIPTS:${PYTHONPATH:-}"

set +e
# Empty-array-safe expansion under set -u: ${array[@]+"${array[@]}"}
# expands to nothing when the array is empty, otherwise to the elements.
PYTHONNOUSERSITE=1 "$VPY" -m pytest \
    ${cov_args[@]+"${cov_args[@]}"} \
    ${extra_args[@]+"${extra_args[@]}"}
rc=$?
set -e

echo ""
if [ "$rc" = "0" ]; then
    echo "[tests] PASS"
    if [ ${#cov_args[@]} -gt 0 ]; then
        echo "[tests] HTML coverage: $HERE/coverage_html/index.html"
    fi
else
    echo "[tests] FAIL (pytest exit code $rc)"
fi

exit "$rc"
