#!/bin/bash
#
# run_tests.sh — entry point for the security-controls test suite.
#
# What this does:
#   1. On first run, pip-installs the test dependencies (one-shot): pytest,
#      pytest-cov, numpy, pyyaml, python-dotenv, requests.
#      numpy is a test-only dependency here: whisper_onnx.py imports it at
#      module scope, and test_whisper_onnx.py imports that module directly,
#      so a missing numpy is a collection error for the whole suite rather
#      than a skip. At runtime the backend is lazily imported and only on
#      Windows ARM64, where onnxruntime pulls numpy in transitively — which
#      is why requirements.txt does not name it.
#   2. Runs the suite under pytest with coverage on the patched .py
#      files (integrity_monitor, plugin_integrity_check,
#      youtube_summarize).
#   3. Generates an HTML coverage report at coverage_html/ alongside.
#   4. Prints a final pass/fail + coverage summary.
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

missing=()
python3 -c "import pytest" 2>/dev/null || missing+=("pytest")
python3 -c "import pytest_cov" 2>/dev/null || missing+=("pytest-cov")
python3 -c "import numpy" 2>/dev/null || missing+=("numpy")
# pyyaml, python-dotenv and requests are runtime deps of the modules under
# test, imported at MODULE scope - so a missing one is a collection error for
# the whole suite, not a skipped file. They live in requirements.txt for the
# vault's venv, but this suite runs against the ambient python3 (it tests a
# repo checkout, no vault install required), so it has to ensure them here.
# A developer machine usually has them already and never notices; a fresh
# clone does not, which is where the documented entry point broke.
python3 -c "import yaml" 2>/dev/null || missing+=("pyyaml")
python3 -c "import dotenv" 2>/dev/null || missing+=("python-dotenv")
python3 -c "import requests" 2>/dev/null || missing+=("requests")

if [ ${#missing[@]} -gt 0 ]; then
    echo "[tests] installing missing test dependencies: ${missing[*]}"
    pip3 install --break-system-packages --quiet "${missing[@]}"
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
python3 -m pytest \
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
