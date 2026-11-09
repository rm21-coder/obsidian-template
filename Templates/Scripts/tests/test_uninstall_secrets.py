"""
test_uninstall_secrets.py — uninstall.sh --secrets removes what the installer
stored (F10, v2.6 assessment, scan 018F33oX33X).

The teardown used to delete only gemini_api_key and obsidian-allowlist-hmac,
so the model key, the Open WebUI key and admin password, and the mail-drop
app password and token survived a teardown the operator believed was
complete. These run the real section 6 of uninstall.sh against a stub
/usr/bin/security store, never the Keychain.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from platform_caps import macos_shell

REPO = Path(__file__).resolve().parents[3]
UNINSTALL = (REPO / "uninstall.sh").read_text(encoding="utf-8")
COMPONENTS = REPO / "installers" / "components"

# Everything the installer writes to the keystore, by lowercased service name.
STORED = ["my_gw_key", "kv_cert_key", "anthropic_api_key", "open_webui_api_key",
          "open_webui_admin_password", "source_mail_app_password",
          "source_mail_token", "azure_kv_client_key", "obsidian-allowlist-hmac"]


def _section6() -> str:
    m = re.search(r"# ---- 6\. Secrets.*?\n(.*?)\n# ---- 7\.", UNINSTALL, re.S)
    assert m, "section 6 (Secrets) not found in uninstall.sh"
    return m.group(1)


INSTALLER_ENV = ("LLM_API_KEY_NAME=MY_GW_KEY\nLLM_BASE_URL=https://gw.example\n"
                 "KEYVAULT_KEY_NAME=KV_CERT_KEY\n")


def _teardown(tmp_path: Path, *, dry_run: int = 0, env_text: str | None = INSTALLER_ENV,
              stored: list[str] | None = None) -> tuple[subprocess.CompletedProcess, Path]:
    store = tmp_path / "store"
    store.write_text("".join(f"{s}\n" for s in (stored or STORED) + ["someone_elses_item"]))
    env = tmp_path / ".env"
    if env_text is not None:
        env.write_bytes(env_text.encode("utf-8"))
    section = tmp_path / "section6.sh"
    section.write_text(_section6())
    script = r'''
set -euo pipefail
source "$REPO/installers/lib/secrets.sh"
SECRETS_ENV="$ENVFILE"
info() { echo "INFO $*"; }; ok() { echo "OK $*"; }; warn() { echo "WARN $*"; }
confirm() { return 1; }                  # never reached: INTERACTIVE=0
section() { echo "SECTION $*"; }
run() { echo "RUN $*"; }
_security() {                            # stub store: one service per line
    local op="$2" svc="" prev=""
    for a in "$@"; do [[ "$prev" == "-s" ]] && svc="$a"; prev="$a"; done
    case "$op" in
        find-generic-password)   grep -qx -- "$svc" "$STORE" ;;
        delete-generic-password) grep -vx -- "$svc" "$STORE" > "$STORE.n" || true
                                 mv "$STORE.n" "$STORE" ;;
        *) echo "unexpected security op $op" >&2; return 2 ;;
    esac
}
DO_SECRETS=1; INTERACTIVE=0; DRY_RUN="$DRY"; KEYCHAIN_TIMEOUT_SECONDS=10
HOME="$FAKEHOME"; USER=tester
source "$SECTION"
'''
    home = tmp_path / "home"
    home.mkdir()
    p = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={**os.environ, "REPO": str(REPO), "ENVFILE": str(env),
             "STORE": str(store), "SECTION": str(section), "DRY": str(dry_run),
             "FAKEHOME": str(home), "NO_COLOR": "1"})
    return p, store


@macos_shell
def test_teardown_removes_every_item_the_installer_stored(tmp_path, allow_subprocess) -> None:
    p, store = _teardown(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    left = store.read_text().split()
    assert left == ["someone_elses_item"], (left, p.stdout)
    for svc in STORED:
        assert f"removed Keychain entry: {svc}" in p.stdout


@macos_shell
def test_dry_run_deletes_nothing_and_names_each_item(tmp_path, allow_subprocess) -> None:
    p, store = _teardown(tmp_path, dry_run=1)
    assert p.returncode == 0, p.stdout + p.stderr
    assert sorted(store.read_text().split()) == sorted(STORED + ["someone_elses_item"])
    for svc in STORED:
        assert f"[dry-run] would delete Keychain entry: {svc}" in p.stdout


def test_every_name_a_component_stores_is_in_the_teardown_list() -> None:
    """The list in installed_keystore_names must not drift from what the
    components write. A name passed as a variable (the gateway key) is read
    from .env by the function itself."""
    lib = (REPO / "installers" / "lib" / "secrets.sh").read_text(encoding="utf-8")
    m = re.search(r"installed_keystore_names\(\) \{(.*?)\n\}", lib, re.S)
    assert m, "installed_keystore_names() not found in installers/lib/secrets.sh"
    listed = set(re.findall(r"[a-z][a-z0-9_-]+", m.group(1)))
    writers = r"(?:migrate_env_secret|prompt_env_secret|keystore_set|_secret_store_set|prompt_keychain_secret)"
    stored = set()
    for f in sorted(COMPONENTS.glob("*.sh")):
        for name in re.findall(writers + r"\s+\"?([A-Z][A-Z0-9_]+)\b",
                               f.read_text(encoding="utf-8")):
            stored.add(name.lower())
    assert stored, "found no keystore writes in installers/components"
    missing = sorted(stored - listed)
    assert not missing, f"stored by a component but not removed by --secrets: {missing}"


def test_uninstall_derives_its_list_rather_than_hard_coding_it() -> None:
    assert "installed_keystore_names" in _section6()
    assert "for svc in gemini_api_key obsidian-allowlist-hmac" not in UNINSTALL


# ---- review R7, R8, R15 -------------------------------------------------------

@macos_shell
@pytest.mark.parametrize("line", [
    'LLM_API_KEY_NAME="MY_GW_KEY"',
    "LLM_API_KEY_NAME='MY_GW_KEY'",
    "export LLM_API_KEY_NAME=MY_GW_KEY",
    "LLM_API_KEY_NAME=MY_GW_KEY   # the gateway",
    "LLM_API_KEY_NAME = MY_GW_KEY",
    "LLM_API_KEY_NAME=OLD_NAME\nLLM_API_KEY_NAME=MY_GW_KEY",   # last one wins
])
@pytest.mark.parametrize("crlf", [False, True])
def test_the_gateway_key_name_is_read_the_way_the_jobs_read_it(
        tmp_path, allow_subprocess, line, crlf) -> None:
    """Review R7: python-dotenv reads all of these as MY_GW_KEY, so the jobs
    use that item; `grep | cut` did not, and the teardown kept it, silently."""
    text = line + "\nKEYVAULT_KEY_NAME=KV_CERT_KEY\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    p, store = _teardown(tmp_path, env_text=text)
    assert p.returncode == 0, p.stdout + p.stderr
    assert store.read_text().split() == ["someone_elses_item"], p.stdout
    assert "removed Keychain entry: my_gw_key" in p.stdout


@macos_shell
def test_a_missing_dotenv_is_said_not_silently_skipped(tmp_path, allow_subprocess) -> None:
    p, store = _teardown(tmp_path, env_text=None)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "not found, so LLM_API_KEY_NAME and KEYVAULT_KEY_NAME" in p.stdout
    assert ("gateway key or Key Vault certificate key stored\nWARN   under a "
            "custom name is NOT checked") in p.stdout


@macos_shell
def test_an_unset_gateway_name_is_said(tmp_path, allow_subprocess) -> None:
    p, _ = _teardown(tmp_path, env_text="LLM_BASE_URL=https://gw.example\n")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "LLM_API_KEY_NAME is not set, so the gateway key is assumed" in p.stdout


@macos_shell
def test_the_keyvault_key_name_is_derived_too(tmp_path, allow_subprocess) -> None:
    """Review R15: deleting the KEYVAULT_KEY_NAME line from
    installed_keystore_names left every test passing."""
    p, store = _teardown(tmp_path)
    assert "removed Keychain entry: kv_cert_key" in p.stdout
    assert "kv_cert_key" not in store.read_text().split()


@macos_shell
def test_project_keys_left_in_dotenv_are_named_never_valued(
        tmp_path, allow_subprocess) -> None:
    """Review R8: the by-hand list must name the keys actually present,
    including the gateway key's own line and ANTHROPIC_API_KEY, which land
    in .env whenever a Keychain write fails."""
    text = (INSTALLER_ENV + "MY_GW_KEY=sk-secret-gw\nANTHROPIC_API_KEY=sk-secret-a\n"
            "export SOURCE_MAIL_TOKEN=tok-secret\nUNRELATED_TOOL_KEY=other\n")
    p, _ = _teardown(tmp_path, env_text=text)
    assert p.returncode == 0, p.stdout + p.stderr
    out = p.stdout
    for name in ("MY_GW_KEY", "ANTHROPIC_API_KEY", "SOURCE_MAIL_TOKEN",
                 "LLM_API_KEY_NAME", "KEYVAULT_KEY_NAME"):
        assert f"INFO     {name}" in out, (name, out)
    assert "UNRELATED_TOOL_KEY" not in out
    assert not any(v in out + p.stderr for v in ("sk-secret", "tok-secret"))


# ---- review R3: a failed write must not leave the old key live ----------------

def _keystore_set(tmp_path: Path, *, helper_ok: bool, has_old: bool,
                  find_rc: int | None = None, wedged: int = 0):
    store = tmp_path / "store"
    store.write_text("my_token\n" if has_old else "")
    env = tmp_path / ".env"
    env.write_text("")
    script = r'''
set -euo pipefail
source "$REPO/installers/lib/secrets.sh"
SECRETS_ENV="$ENVFILE"; SECRETS_DIR="$(dirname "$ENVFILE")"
info() { echo "INFO $*"; }; ok() { echo "OK $*"; }; warn() { echo "WARN $*"; }
err() { echo "ERR $*"; }
is_macos() { return 0; }
install() { cp "$3" "$4"; }              # env_set uses install -m 0600
_secret_store_set() {
    echo "secret_store: MY_TOKEN: value cannot be written safely" >&2
    [[ "$HELPER_OK" -eq 1 ]]
}
_security() {
    local op="$2" svc="" prev=""
    for a in "$@"; do [[ "$prev" == "-s" ]] && svc="$a"; prev="$a"; done
    case "$op" in
        find-generic-password)
            if [[ -n "$FIND_RC" ]]; then return "$FIND_RC"; fi
            grep -qx -- "$svc" "$STORE" && return 0 || return 44 ;;
        delete-generic-password) grep -vx -- "$svc" "$STORE" > "$STORE.n" || true
                                 mv "$STORE.n" "$STORE" ;;
    esac
}
KEYCHAIN_WEDGED="$WEDGED"; KEYCHAIN_TIMEOUT_SECONDS=10; USER=tester
keystore_set MY_TOKEN new-value
echo "EXIT $?"
'''
    p = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={**os.environ, "REPO": str(REPO), "ENVFILE": str(env), "STORE": str(store),
             "HELPER_OK": "1" if helper_ok else "0",
             "FIND_RC": "" if find_rc is None else str(find_rc),
             "WEDGED": str(wedged), "NO_COLOR": "1"})
    return p, store, env


@macos_shell
def test_a_failed_write_removes_the_older_keychain_item(tmp_path, allow_subprocess) -> None:
    p, store, env = _keystore_set(tmp_path, helper_ok=False, has_old=True)
    assert "EXIT 0" in p.stdout, p.stdout + p.stderr
    assert store.read_text().split() == []
    assert "MY_TOKEN=new-value" in env.read_text()
    assert "removed the older MY_TOKEN from the Keychain" in p.stdout


@macos_shell
@pytest.mark.parametrize("find_rc,wedged", [(36, 0), (142, 0), (None, 1)])
def test_a_failed_write_that_cannot_clear_the_old_item_says_the_old_one_is_live(
        tmp_path, allow_subprocess, find_rc, wedged) -> None:
    p, store, env = _keystore_set(tmp_path, helper_ok=False, has_old=True,
                                  find_rc=find_rc, wedged=wedged)
    assert "EXIT 0" in p.stdout, p.stdout + p.stderr
    assert "keep using THAT value, not the new one" in p.stdout
    assert "secret_store.py rm MY_TOKEN" in p.stdout


@macos_shell
def test_a_failed_write_with_no_older_item_is_quiet(tmp_path, allow_subprocess) -> None:
    p, _, env = _keystore_set(tmp_path, helper_ok=False, has_old=False)
    assert "EXIT 0" in p.stdout, p.stdout + p.stderr
    assert "older MY_TOKEN" not in p.stdout and "THAT value" not in p.stdout
    assert "MY_TOKEN=new-value" in env.read_text()


# ---- re-review S4, S5 -----------------------------------------------------------

@macos_shell
def test_an_unreadable_dotenv_does_not_abort_the_teardown(tmp_path, allow_subprocess) -> None:
    """Review S4: under pipefail an unreadable .env ended the script before
    any Keychain entry was removed."""
    import os as _os
    if _os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    env = tmp_path / ".env"
    env.write_text(INSTALLER_ENV)
    env.chmod(0)
    try:
        p, store = _teardown(tmp_path, env_text=None)
    finally:
        env.chmod(0o600)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "exists but cannot be read" in p.stdout
    left = store.read_text().split()
    # The fixed-name items are still removed; the configurable two cannot be.
    assert sorted(left) == ["kv_cert_key", "my_gw_key", "someone_elses_item"]


@macos_shell
def test_a_lower_case_gateway_name_lists_its_own_line(tmp_path, allow_subprocess) -> None:
    """Review S5: the name was upper-cased before matching, so its own line
    (as python-dotenv reads it, case and all) was never listed."""
    text = "LLM_API_KEY_NAME=my_gw_key\nmy_gw_key=sk-secret\nYOUTUBE_MODEL=m\n" \
           "OBSIDIAN_COLLECTION_ID=c\n"
    p, _ = _teardown(tmp_path, env_text=text)
    assert p.returncode == 0, p.stdout + p.stderr
    for name in ("my_gw_key", "YOUTUBE_MODEL", "OBSIDIAN_COLLECTION_ID"):
        assert f"INFO     {name}" in p.stdout, (name, p.stdout)
    assert "sk-secret" not in p.stdout + p.stderr
