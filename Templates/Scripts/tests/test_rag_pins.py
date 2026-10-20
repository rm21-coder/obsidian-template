"""installers/rag-pins.json and rag_pins.py: the local RAG stack's image and
models are pinned (2026-10-17). Until then Open WebUI came from its moving
`:main` development tag and Ollama models by name, so whatever upstream
published next is what installed, for the component that holds the whole
vault as a search collection."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PINS = REPO / "installers" / "rag-pins.json"
WIN = REPO / "Templates" / "Scripts" / "windows"
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")

spec = importlib.util.spec_from_file_location("rag_pins", REPO / "installers" / "lib" / "rag_pins.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def test_pins_name_a_release_by_digest_not_a_moving_tag() -> None:
    pins = json.loads(PINS.read_text())
    w = pins["open_webui"]
    assert w["image"] == "ghcr.io/open-webui/open-webui"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", w["tag"]), "a release tag, never main/latest"
    assert SHA.match(w["digest"])
    assert rp.image_ref(pins) == f"{w['image']}:{w['tag']}@{w['digest']}"
    assert pins["ollama_models"] and all(SHA.match(d) for d in pins["ollama_models"].values())


def _setup_ollama_models() -> list:
    src = (REPO / "Templates" / "Scripts" / "setup-ollama.sh").read_text()
    return re.findall(r'MODELS\+?=\("([^"]+)"\)', src)


def test_every_model_the_setup_scripts_pull_is_pinned() -> None:
    pinned = json.loads(PINS.read_text())["ollama_models"]
    mac = _setup_ollama_models()
    win = re.search(r"\[string\]\$Model = '([^']+)'", (WIN / "setup-rag.ps1").read_text()).group(1)
    assert len(mac) >= 1
    for model in mac + [win]:
        assert model in pinned, model


def test_no_shipped_script_pulls_open_webui_by_a_moving_tag() -> None:
    for p in (REPO / "installers" / "components" / "50-llm-rag.sh", WIN / "common.ps1",
              WIN / "setup-rag.ps1", REPO / "update.sh"):
        assert "open-webui:main" not in p.read_text(encoding="utf-8"), p.name
    comp = (REPO / "installers" / "components" / "50-llm-rag.sh").read_text()
    assert 'rag_pins.py" image)' in comp and '"$OPEN_WEBUI_IMAGE"' in comp
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    assert "rag-pins.json" in common and "function Test-OpenWebUIImagePin" in common
    assert "Test-OllamaModelPin $Model" in (WIN / "setup-rag.ps1").read_text(encoding="utf-8")
    assert "Test-OpenWebUIImagePin" in (WIN / "update.ps1").read_text(encoding="utf-8")
    assert "rag_pins.py\" image-digest" in (REPO / "update.sh").read_text()
    assert 'verify-models "${MODELS[@]}"' in (REPO / "Templates" / "Scripts" / "setup-ollama.sh").read_text()


def test_compare_reports_each_state() -> None:
    pins = {"ollama_models": {"a:1": "sha256:" + "a" * 64, "b:1": "sha256:" + "b" * 64,
                              "c:1": "sha256:" + "c" * 64}}
    installed = {"a:1": "sha256:" + "a" * 64, "b:1": "sha256:" + "f" * 64, "z:9": "sha256:" + "0" * 64}
    assert rp.compare(pins, installed, []) == [("a:1", "OK"), ("b:1", "CHANGED")]
    assert rp.compare(pins, installed, ["c:1", "z:9"]) == [("c:1", "NOT INSTALLED"), ("z:9", "UNPINNED")]


def test_verify_models_exits_1_only_when_a_model_changed(monkeypatch, capsys) -> None:
    pinned = json.loads(PINS.read_text())["ollama_models"]
    name = next(iter(pinned))
    monkeypatch.setattr(rp, "installed_digests", lambda host: {name: pinned[name]})
    assert rp.main(["verify-models"]) == 0
    monkeypatch.setattr(rp, "installed_digests", lambda host: {name: "sha256:" + "0" * 64})
    assert rp.main(["verify-models"]) == 1
    assert "CHANGED" in capsys.readouterr().out


def test_fetches_are_held_to_loopback_and_the_two_registries() -> None:
    import pytest
    with pytest.raises(ValueError):
        rp.installed_digests("http://192.168.1.5:11434")
    with pytest.raises(ValueError):
        rp.installed_digests("file:///etc/passwd")
    with pytest.raises(ValueError):
        rp._get("https://evil.example/v2/x", {})
    with pytest.raises(ValueError):
        rp._get("https://ghcr.io.evil.example/v2/x", {})


# ---- 5268557 / N1: Open WebUI login, CORS and signup ------------------------
# Loopback is not private from the browser: a page the operator opens can
# reach 127.0.0.1:3000, so the installers start Open WebUI with login on, CORS
# held to the loopback origins and signup closed, and report a container that
# was created without them.

def _inspect(*env: str) -> list:
    return [{"Name": "/open-webui", "Config": {"Env": list(env)}}]


HARDENED = ("OLLAMA_BASE_URL=http://host.docker.internal:11434", "WEBUI_AUTH=True",
            "CORS_ALLOW_ORIGIN=" + rp.WEBUI_ORIGINS, "ENABLE_SIGNUP=False")


def test_a_hardened_container_has_no_findings() -> None:
    assert rp.container_findings(_inspect(*HARDENED)) == []
    # docker inspect of a single object, not a list, reads the same
    assert rp.container_findings(_inspect(*HARDENED)[0]) == []


def test_the_live_n1_container_is_reported() -> None:
    """The maintainer's container (N1): WEBUI_AUTH=false set by hand, no CORS."""
    found = rp.container_findings(_inspect(
        "OLLAMA_BASE_URL=http://host.docker.internal:11434", "WEBUI_AUTH=false", "ENABLE_API_KEY=true"))
    assert found[0].startswith("WEBUI_AUTH=false: no login")
    assert found[1] == "CORS_ALLOW_ORIGIN is unset, so Open WebUI allows every origin (*)"


def test_every_spelling_of_auth_off_is_reported() -> None:
    for value in ("false", "False", "FALSE", "0", "no", "off", ""):
        found = rp.container_findings(_inspect("WEBUI_AUTH=" + value, "CORS_ALLOW_ORIGIN=" + rp.WEBUI_ORIGINS))
        assert found and found[0].startswith(f"WEBUI_AUTH={value}: no login"), value
    # unset is Open WebUI's default, which is auth on
    assert rp.container_findings(_inspect("CORS_ALLOW_ORIGIN=" + rp.WEBUI_ORIGINS)) == []


def test_a_wildcard_or_empty_cors_is_reported() -> None:
    for value in ("*", "http://localhost:3000;*", " * ", ""):
        found = rp.container_findings(_inspect("WEBUI_AUTH=True", "CORS_ALLOW_ORIGIN=" + value))
        assert found == [f"CORS_ALLOW_ORIGIN={value} allows every origin"], value


def test_check_container_cli_exit_codes(monkeypatch, capsys) -> None:
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_inspect(*HARDENED))))
    assert rp.main(["check-container"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_inspect("WEBUI_AUTH=false"))))
    assert rp.main(["check-container"]) == 1
    assert "WEBUI_AUTH=false: no login" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
    assert rp.main(["check-container"]) == 2
    assert "no open-webui container to check" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("sha256:imageid"))
    assert rp.main(["check-container"]) == 2
    assert "unreadable docker inspect output" in capsys.readouterr().err


def test_webui_state_classifies_api_config() -> None:
    assert rp.webui_state({"features": {"auth": True, "enable_signup": False}}) == "OK"
    assert rp.webui_state({"onboarding": True, "features": {"auth": True, "enable_signup": False}}) == "NO-ADMIN"
    assert rp.webui_state({"features": {"auth": True, "enable_signup": True}}) == "SIGNUP-OPEN"
    assert rp.webui_state({"features": {"auth": False, "enable_signup": False}}) == "AUTH-OFF"
    # anything it cannot read is UNKNOWN, never OK
    assert rp.webui_state({"features": {"auth": True}}) == "UNKNOWN"
    assert rp.webui_state({"name": "Open WebUI"}) == "UNKNOWN"
    assert rp.webui_state(None) == "UNKNOWN"


def test_webui_state_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rp, "fetch_webui_config",
                        lambda url, wait: {"features": {"auth": True, "enable_signup": True}})
    assert rp.main(["webui-state"]) == 1
    assert capsys.readouterr().out.strip() == "SIGNUP-OPEN"

    def down(url, wait):
        raise OSError("connection refused")
    monkeypatch.setattr(rp, "fetch_webui_config", down)
    assert rp.main(["webui-state"]) == 2
    assert "open webui not reachable" in capsys.readouterr().err


def test_webui_signin_reads_credentials_from_stdin_not_argv(monkeypatch) -> None:
    import io
    seen = {}
    monkeypatch.setattr(rp, "fetch_webui_config", lambda url, wait: {})

    def signin(email, password, url):
        seen.update(email=email, password=password)
        return password == "s3cret-value"
    monkeypatch.setattr(rp, "webui_signin_ok", signin)
    monkeypatch.setattr("sys.stdin", io.StringIO("admin@example.com\ns3cret-value\n"))
    assert rp.main(["webui-signin", "--wait", "1"]) == 0
    assert seen == {"email": "admin@example.com", "password": "s3cret-value"}
    monkeypatch.setattr("sys.stdin", io.StringIO("admin@example.com\nwrong\n"))
    assert rp.main(["webui-signin"]) == 1


def test_open_webui_calls_are_held_to_loopback() -> None:
    import pytest
    for url in ("http://192.168.1.5:3000", "http://localhost.evil.example:3000", "file:///etc/passwd"):
        with pytest.raises(ValueError, match="refusing a non-loopback Open WebUI URL"):
            rp.fetch_webui_config(url)
        with pytest.raises(ValueError, match="refusing a non-loopback Open WebUI URL"):
            rp.webui_signin_ok("a@example.com", "x", url)


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_both_installers_start_open_webui_hardened() -> None:
    comp = (REPO / "installers" / "components" / "50-llm-rag.sh").read_text()
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    mac = _between(comp, "start_open_webui() {", '"$OPEN_WEBUI_IMAGE"')
    win = _between(common, "function Start-OpenWebUIContainer", "$script:OpenWebUIImage\n")
    for block in (mac, win):
        assert "-e 'WEBUI_AUTH=True'" in block
        assert f"-e 'CORS_ALLOW_ORIGIN={rp.WEBUI_ORIGINS}'" in block
        assert "-p 127.0.0.1:3000:8080" in block
    assert '-e "ENABLE_SIGNUP=$OPEN_WEBUI_SIGNUP"' in mac
    assert 'OPEN_WEBUI_SIGNUP="${OPEN_WEBUI_SIGNUP:-False}"' in comp
    assert '-e "ENABLE_SIGNUP=$signup"' in win
    assert "$signup = if ($AllowSignup) { 'True' } else { 'False' }" in win
    # the headless admin's credentials go through an env-file, never argv
    for text in (comp, common):
        assert "WEBUI_ADMIN_PASSWORD=" in text and "--env-file" in text
        assert "-e WEBUI_ADMIN_PASSWORD" not in text and "-e 'WEBUI_ADMIN_PASSWORD" not in text
    assert "_secret_store_set OPEN_WEBUI_ADMIN_PASSWORD" in comp
    assert "umask 077" in comp and 'chmod 600 "$env_file"' in comp
    assert comp.index('start_open_webui "$env_file"') < comp.index('        rm -f "$env_file"')
    assert "set OPEN_WEBUI_ADMIN_PASSWORD" in common
    assert "Remove-Item -LiteralPath $envFile" in common


def test_both_installers_and_updaters_report_an_unhardened_container() -> None:
    comp = (REPO / "installers" / "components" / "50-llm-rag.sh").read_text()
    assert comp.count("report_open_webui_hardening") >= 4  # definition + every branch
    assert "check-container" in comp and "webui-state" in comp
    assert "check-container" in (REPO / "update.sh").read_text()
    common = (WIN / "common.ps1").read_text(encoding="utf-8")
    assert "function Test-OpenWebUIHardening" in common and "function Get-OpenWebUIState" in common
    assert "Test-OpenWebUIHardening" in (WIN / "update.ps1").read_text(encoding="utf-8")
    setup = (WIN / "setup-rag.ps1").read_text(encoding="utf-8")
    assert "Test-OpenWebUIHardening" in setup and "Start-OpenWebUIFresh" in setup
    # a closed signup no longer reads as "an admin already exists"
    assert "$cfg.features.enable_signup" not in setup
