"""installers/lib/secrets.sh: moving a secret an older install left in .env
into the Keychain (migrate_env_secret), and the mail-drop component keeping
its two secrets out of .env.

The shell helpers run for real in bash, sourced into a throwaway HOME, with
is_macos, _security and _secret_store_set replaced by stubs, so no Keychain
is touched. Until 2026-10-17 39-source-mail.sh wrote SOURCE_MAIL_TOKEN, the
transport's shared HMAC key, to .env in plain text.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from platform_caps import macos_shell

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "installers" / "lib"
COMPONENT = REPO / "installers" / "components" / "39-source-mail.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Stubs: the fake Keychain is a directory with one file per service.
HARNESS = r'''
set -euo pipefail
ok()   { echo "OK: $*"; }
warn() { echo "WARN: $*"; }
info() { :; }
err()  { echo "ERR: $*"; }
source "$LIB/secrets.sh"
is_macos() { true; }
KC="$HOME/kc"; mkdir -p "$KC"
_security() {
    shift   # the timeout
    local svc=""
    while [[ $# -gt 0 ]]; do
        case "$1" in -s) svc="$2"; shift 2;; *) shift;; esac
    done
    [[ -f "$KC/$svc" ]] || return 44
    cat "$KC/$svc"
}
_secret_store_set() {
    [[ "${KC_FAILS:-0}" -eq 1 ]] && return 1
    printf '%s' "$2" > "$KC/$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
}
'''


def run(tmp_path: Path, script: str, env_lines: str = "", kc: dict | None = None,
        kc_fails: bool = False) -> tuple[str, str, dict]:
    home = tmp_path / "home"
    (home / "dev" / "secrets").mkdir(parents=True)
    env_file = home / "dev" / "secrets" / ".env"
    env_file.write_text(env_lines)
    env_file.chmod(0o600)
    (home / "kc").mkdir()
    for svc, val in (kc or {}).items():
        (home / "kc" / svc).write_text(val)
    p = subprocess.run(
        ["bash", "-c", HARNESS + script],
        env={"HOME": str(home), "LIB": str(LIB), "USER": "u", "PATH": "/usr/bin:/bin",
             "KC_FAILS": "1" if kc_fails else "0"},
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    store = {f.name: f.read_text() for f in (home / "kc").iterdir()}
    return p.stdout, env_file.read_text(), store


@macos_shell
def test_moves_a_dotenv_secret_into_the_keychain(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       "SOURCE_MAIL_USER=a@b.example\nSOURCE_MAIL_TOKEN=abc123\n")
    assert kc == {"source_mail_token": "abc123"}
    assert "SOURCE_MAIL_TOKEN" not in env and "SOURCE_MAIL_USER=a@b.example" in env
    assert "moved from" in out


@macos_shell
def test_keeps_dotenv_when_the_keychain_write_fails(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       "SOURCE_MAIL_TOKEN=abc123\n", kc_fails=True)
    assert kc == {} and "SOURCE_MAIL_TOKEN=abc123" in env
    assert "Keychain unavailable" in out


@macos_shell
def test_removes_a_duplicate_only_when_the_values_match(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       "SOURCE_MAIL_TOKEN=abc123\n", kc={"source_mail_token": "abc123"})
    assert "SOURCE_MAIL_TOKEN" not in env and kc == {"source_mail_token": "abc123"}
    assert "removed the plaintext copy" in out


@macos_shell
def test_never_switches_secrets_when_the_values_differ(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       "SOURCE_MAIL_TOKEN=in-use\n", kc={"source_mail_token": "stale"})
    assert "SOURCE_MAIL_TOKEN=in-use" in env and kc == {"source_mail_token": "stale"}
    assert "different values" in out


@macos_shell
def test_a_secret_already_only_in_the_keychain_is_left_alone(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       "SOURCE_MAIL_USER=a@b.example\n", kc={"source_mail_token": "abc"})
    assert out == "" and kc == {"source_mail_token": "abc"}


def test_mail_component_keeps_its_secrets_out_of_dotenv() -> None:
    src = COMPONENT.read_text(encoding="utf-8")
    assert "env_set SOURCE_MAIL_TOKEN" not in src
    assert "env_set SOURCE_MAIL_APP_PASSWORD" not in src
    assert 'keystore_set SOURCE_MAIL_TOKEN "$TOKEN"' in src
    assert "keystore_has SOURCE_MAIL_TOKEN" in src
    assert src.index("migrate_env_secret SOURCE_MAIL_TOKEN") < src.index("keystore_has SOURCE_MAIL_TOKEN")
    assert "migrate_env_secret SOURCE_MAIL_APP_PASSWORD" in src
    # Telling the operator to grep .env for the token would print it from a
    # file it no longer lives in.
    assert "grep '^SOURCE_MAIL_TOKEN=' " not in src
