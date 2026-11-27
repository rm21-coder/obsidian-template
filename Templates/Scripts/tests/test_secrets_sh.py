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
    if [[ -n "${KC_FIND_RC:-}" ]]; then return "$KC_FIND_RC"; fi
    [[ -f "$KC/$svc" ]] || return 44
    cat "$KC/$svc"
}
_secret_store_set() {
    [[ "${KC_FAILS:-0}" -eq 1 ]] && return 1
    [[ "${KC_FAILS:-0}" -eq 3 ]] && return 3
    printf '%s' "$2" > "$KC/$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
}
'''


def run(tmp_path: Path, script: str, env_lines: str = "", kc: dict | None = None,
        kc_fails: bool | int = False, find_rc: int | None = None) -> tuple[str, str, dict]:
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
             "KC_FAILS": str(int(kc_fails)),
             "KC_FIND_RC": "" if find_rc is None else str(find_rc)},
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
    assert "keystore_plan SOURCE_MAIL_TOKEN" in src
    assert src.index("migrate_env_secret SOURCE_MAIL_TOKEN") < src.index("keystore_plan SOURCE_MAIL_TOKEN")
    assert "migrate_env_secret SOURCE_MAIL_APP_PASSWORD" in src
    # Telling the operator to grep .env for the token would print it from a
    # file it no longer lives in.
    assert "grep '^SOURCE_MAIL_TOKEN=' " not in src


# ---- review S3: an unreadable Keychain is not an empty one --------------------

@macos_shell
@pytest.mark.parametrize("rc", [36, 51, 1])
def test_migrate_moves_nothing_when_the_keychain_cannot_be_read(
        tmp_path, allow_subprocess, rc):
    """Moving the .env value in would replace a stored value with it, which
    is what KV-1 exists to stop."""
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       "SOURCE_MAIL_TOKEN=from-dotenv\n",
                       kc={"source_mail_token": "stored"}, find_rc=rc)
    assert "SOURCE_MAIL_TOKEN=from-dotenv" in env
    assert kc == {"source_mail_token": "stored"}
    assert f"the Keychain could not be read (exit {rc}); left it in" in out


@macos_shell
def test_migrate_latches_on_a_timeout(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, 'migrate_env_secret SOURCE_MAIL_TOKEN; '
                       'echo "WEDGED=$KEYCHAIN_WEDGED"',
                       "SOURCE_MAIL_TOKEN=from-dotenv\n", find_rc=142)
    assert "WEDGED=1" in out and "SOURCE_MAIL_TOKEN=from-dotenv" in env


@macos_shell
@pytest.mark.parametrize("find_rc,kc,env_lines,want", [
    (None, {"k1": "v"}, "", 0),             # held in the Keychain
    (None, {}, "K1=v\n", 0),                # Keychain says absent, .env has it
    (None, {}, "", 1),                       # nowhere
    (36, {"k1": "v"}, "K1=v\n", 2),         # unreadable: not "held via .env"
    (142, {}, "", 2),
])
def test_keystore_has_draws_the_python_line(tmp_path, allow_subprocess,
                                            find_rc, kc, env_lines, want):
    out, _, _ = run(tmp_path, 'keystore_has K1 && rc=0 || rc=$?; echo "RC=$rc"',
                    env_lines, kc=kc, find_rc=find_rc)
    assert f"RC={want}" in out


@macos_shell
def test_prompt_neither_prompts_nor_writes_when_unreadable(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path,
                       'FORCE=1; prompt_env_secret K1 "desc" "https://example.invalid" < /dev/null',
                       "", kc={"k1": "stored"}, find_rc=36)
    assert kc == {"k1": "stored"} and env == ""
    assert "the Keychain could not be read; not prompting," in out


def test_the_token_step_does_not_generate_when_unreadable() -> None:
    """Generating a token over an unreadable Keychain orphans the one the
    phone holds. The skip must come before the generator."""
    src = COMPONENT.read_text(encoding="utf-8")
    assert "keystore_plan SOURCE_MAIL_TOKEN" in src
    assert src.index('[[ "$KEYSTORE_PLAN" == skip ]]') < src.index("secrets.choice")
    assert "not generating a new token" in src


@macos_shell
@pytest.mark.parametrize("find_rc,kc,force,want", [
    (None, {"source_mail_token": "t"}, 0, "keep"),
    (None, {"source_mail_token": "t"}, 1, "create"),
    (None, {}, 0, "create"),
    (36, {"source_mail_token": "t"}, 0, "skip"),
    (36, {"source_mail_token": "t"}, 1, "skip"),      # --force never overrides
    (142, {}, 1, "skip"),
])
def test_keystore_plan_is_what_the_token_step_acts_on(tmp_path, allow_subprocess,
                                                      find_rc, kc, force, want):
    """T3: the token step's decision, run (not read from the source). Only
    "create" leads to a new token; the component branches on this value."""
    out, _, store = run(tmp_path, f'FORCE={force}; keystore_plan SOURCE_MAIL_TOKEN; '
                        'echo "PLAN=$KEYSTORE_PLAN"', kc=kc, find_rc=find_rc)
    assert f"PLAN={want}" in out
    assert store == kc


@macos_shell
def test_a_hang_inside_keystore_has_is_explained_and_names_the_way_out(
        tmp_path, allow_subprocess):
    """T2: _keychain_state sent the latch's explanation to /dev/null, and the
    old text said secrets "fall back to .env", which no longer happens."""
    script = r'''
info() { echo "INFO: $*"; }
_security() { _keychain_latch "find-generic-password (10s)"; return 142; }
keystore_plan K1; keystore_plan K2
'''
    out, env, kc = run(tmp_path, script, "K2=v\n")
    assert "ERR:   Keychain call timed out: the Keychain read of k1" in out
    assert "every later secret is skipped" in out
    assert "secret_store.py rm <NAME>" in out
    assert "fall back" not in out
    assert "K2: skipped; the Keychain is not answering this run" in out
    assert env == "K2=v\n" and kc == {}


@macos_shell
def test_a_failed_write_shows_only_the_helper_s_own_lines(tmp_path, allow_subprocess):
    """Review S8: removing the loop that shows the helper's reason failed no
    test, because every test stubbed _secret_store_set."""
    root = tmp_path / "repo"
    helper = root / "Templates" / "Scripts" / "secret_store.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        "print('Traceback: value=sk-should-not-show', file=sys.stderr)\n"
        "print('secret_store: K1: value cannot be written safely', file=sys.stderr)\n"
        "sys.exit(1)\n")
    script = (r'''
set -euo pipefail
ok() { echo "OK: $*"; }; warn() { echo "WARN: $*"; }; info() { :; }; err() { echo "ERR: $*"; }
source "$LIB/secrets.sh"
REPO_ROOT="$ROOT"
_secret_store_set K1 sk-the-value && echo SET || echo NOTSET
''')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"HOME": str(tmp_path), "LIB": str(LIB), "ROOT": str(root),
                            "USER": "u", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    assert "NOTSET" in p.stdout, p.stdout + p.stderr
    assert "WARN:   secret_store: K1: value cannot be written safely" in p.stdout
    assert "sk-" not in p.stdout + p.stderr


@macos_shell
@pytest.mark.parametrize("env_lines,kc,find_rc,want", [
    ("", {"k1": "v"}, None, "OK:   K1 resolves (Keychain, or .env where the Keychain lacks it)"),
    ("K1=v\n", {}, None, "OK:   K1 resolves"),
    ("", {}, None, "WARN:   K1 not resolvable"),
    ("K1=v\n", {"k1": "v"}, 36, "WARN:   K1: the Keychain could not be read, so the scheduled"),
    ("KEYVAULT_URL=https://x.vault.azure.net/\n", {}, 36, "INFO:   Key Vault is on: check K1 with"),
    ("# KEYVAULT_URL=https://x.vault.azure.net/\n", {"k1": "v"}, None, "OK:   K1 resolves"),
])
def test_verify_reports_each_resolution_state(tmp_path, allow_subprocess,
                                              env_lines, kc, find_rc, want):
    """T3: 90-verify's key report, every branch run, including that the vault
    case never reads the Keychain (find_rc 36 would say "could not be read")."""
    out, _, _ = run(tmp_path, 'info() { echo "INFO: $*"; }; report_key_resolution K1 /s',
                    env_lines, kc=kc, find_rc=find_rc)
    assert want in out, out
    if "Key Vault" in want:
        assert "could not be read" not in out



# ---------- F-20261125: Key Vault on -------------------------------------------

VAULT_ENV = "KEYVAULT_URL=https://x.vault.azure.net/\n"


@macos_shell
def test_a_vault_refusal_never_falls_back_to_dotenv(tmp_path, allow_subprocess):
    """secret_store.py set exits 3 when Key Vault holds the name. Taking that
    for a Keychain failure would write the value to .env in plain text."""
    out, env, kc = run(tmp_path, "keystore_set SOURCE_MAIL_TOKEN abc123", VAULT_ENV,
                       kc_fails=3)
    assert "abc123" not in env and kc == {}
    assert "WARN:   SOURCE_MAIL_TOKEN not stored: Key Vault is on" in out
    assert "falling back" not in out


@macos_shell
def test_a_keychain_failure_still_falls_back_to_dotenv(tmp_path, allow_subprocess):
    """The other side of the line above: exit 1 keeps the old fallback."""
    out, env, kc = run(tmp_path, "keystore_set OPEN_WEBUI_API_KEY abc123", "",
                       kc_fails=True)
    assert "OPEN_WEBUI_API_KEY=abc123" in env and "falling back" in out


@macos_shell
def test_migrate_says_why_when_the_vault_refuses(tmp_path, allow_subprocess):
    out, env, kc = run(tmp_path, "migrate_env_secret SOURCE_MAIL_TOKEN",
                       VAULT_ENV + "SOURCE_MAIL_TOKEN=abc123\n", kc_fails=3)
    assert "SOURCE_MAIL_TOKEN=abc123" in env and kc == {}
    assert "WARN:   SOURCE_MAIL_TOKEN: Key Vault is on, so this .env copy is not used" in out
    assert "Keychain unavailable" not in out


@macos_shell
@pytest.mark.parametrize("key,env_extra,held", [
    ("SOURCE_MAIL_TOKEN", "", True),
    ("SOURCE_MAIL_APP_PASSWORD", "", True),
    ("OPEN_WEBUI_API_KEY", "", True),
    ("ANTHROPIC_API_KEY", "", True),
    ("GW_KEY", "LLM_API_KEY_NAME=gw_key\n", True),
    ("ANTHROPIC_API_KEY", "LLM_API_KEY_NAME=gw_key\n", False),
    ("OPEN_WEBUI_ADMIN_PASSWORD", "", False),
])
def test_keystore_plan_skips_only_what_the_vault_holds(tmp_path, allow_subprocess,
                                                       key, env_extra, held):
    """No prompt and no generated token for a secret the vault holds, since a
    value stored here would never be used. Other names are planned as before."""
    out, _, _ = run(tmp_path, f'keystore_plan {key}; echo "PLAN=$KEYSTORE_PLAN"',
                    VAULT_ENV + env_extra)
    if held:
        assert "PLAN=skip" in out
        assert f"OK:   {key}: Key Vault is on and holds it" in out
    else:
        assert "PLAN=create" in out and "Key Vault is on" not in out


@macos_shell
def test_keystore_plan_with_the_vault_off_is_unchanged(tmp_path, allow_subprocess):
    out, _, _ = run(tmp_path, 'keystore_plan SOURCE_MAIL_TOKEN; echo "PLAN=$KEYSTORE_PLAN"',
                    "# KEYVAULT_URL=https://x.vault.azure.net/\n")
    assert "PLAN=create" in out and "Key Vault" not in out


def test_the_vault_name_list_matches_secret_store() -> None:
    """vault_holds (bash) and secret_store._vault_names must name the same
    secrets: a name only one of them lists is prompted for and refused, or
    stored where no job reads it."""
    import re
    import secret_store
    text = (LIB / "secrets.sh").read_text()
    body = text[text.index("vault_holds() {"):]
    body = body[:body.index("\n}\n")]
    listed = set(re.search(r'"\$gw"\|([A-Z_|]+)\)', body).group(1).split("|"))
    assert listed == set(secret_store._STATUS_NAMES)
    assert "ANTHROPIC_API_KEY" in body


@macos_shell
def test_the_real_store_helper_passes_the_vault_refusal_through(tmp_path, allow_subprocess):
    """The stubs above return 3 themselves; this runs the real
    _secret_store_set against a helper that exits 3, as secret_store.py does."""
    root = tmp_path / "repo"
    helper = root / "Templates" / "Scripts" / "secret_store.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "import sys\n"
        "print('secret_store: K1: not stored. Key Vault is on', file=sys.stderr)\n"
        "sys.exit(3)\n")
    script = (r'''
set -euo pipefail
ok() { echo "OK: $*"; }; warn() { echo "WARN: $*"; }; info() { :; }; err() { echo "ERR: $*"; }
source "$LIB/secrets.sh"
REPO_ROOT="$ROOT"
rc=0; _secret_store_set K1 the-value || rc=$?
echo "RC=$rc"
''')
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"HOME": str(tmp_path), "LIB": str(LIB), "ROOT": str(root),
                            "USER": "u", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    assert "RC=3" in p.stdout, p.stdout + p.stderr
    assert "WARN:   secret_store: K1: not stored. Key Vault is on" in p.stdout
