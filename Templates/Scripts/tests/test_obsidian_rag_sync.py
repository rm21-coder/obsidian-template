"""test_obsidian_rag_sync.py — regression cover for the RAG sync client.

Every test here pins behaviour that was, at some point, wrong in a way that
silently removed notes from the Open WebUI index while the vault on disk
looked perfectly healthy. That is the failure mode worth guarding: the sync
reports PASS, the notes are gone, and nothing surfaces it until someone asks
the model a question it should have been able to answer.

The module under test is hyphenated (`obsidian-rag-sync.py`), so it cannot be
`import`ed by name and is loaded via spec_from_file_location instead.

Import-time side effects it has, all of which the fixture must neutralise
BEFORE the module executes:
  - reads OPEN_WEBUI_API_KEY from the real Keychain via secret_store
  - mkdir's the real ~/.local/share/obsidian-rag-sync/
  - attaches a logging FileHandler to a real sync.log
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import requests


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture
def rag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts_dir: Path):
    """Import obsidian-rag-sync.py against a throwaway HOME, vault and state.

    STATE_DIR is derived from Path.home() at import time, so HOME has to be
    redirected before the module executes — after that it is baked in.
    """
    home = tmp_path / "home"
    vault = home / "Obsidian"
    vault.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OBSIDIAN_VAULT", str(vault))
    monkeypatch.setenv("OPEN_WEBUI_URL", "http://webui.invalid")
    monkeypatch.setenv("OBSIDIAN_COLLECTION_ID", "test-collection")

    # Never touch the real Keychain: secret_store is imported by the module
    # at exec time, so patch the attribute it will bind to.
    import secret_store
    monkeypatch.setattr(secret_store, "get_secret", lambda name: "test-key")

    path = scripts_dir / "obsidian-rag-sync.py"
    spec = importlib.util.spec_from_file_location("obsidian_rag_sync", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["obsidian_rag_sync"] = mod
    spec.loader.exec_module(mod)

    assert Path(mod.STATE_DIR).is_relative_to(home), (
        "state dir escaped the tmp HOME — test would pollute real state")
    yield mod
    sys.modules.pop("obsidian_rag_sync", None)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int = 200, text: str = "",
                 payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error")


# ---------------------------------------------------------------------------
# The quarantine rule.
#
# The bug: record_failure() writes an entry on failure #1, and the skip filter
# only checked that an entry existed. MAX_FAILURES gated nothing but a log
# line, so one dropped connection removed a note from the index until its
# content happened to change.
# ---------------------------------------------------------------------------

def test_single_failure_does_not_block(rag):
    """The regression. One transient failure must NOT exile a note."""
    entry = {"hash": "abc", "failures": 1}
    assert rag.quarantine_blocks(entry, "abc") is False


def test_blocks_only_at_max_failures(rag):
    """The threshold is MAX_FAILURES, and it is inclusive."""
    for n in range(1, rag.MAX_FAILURES):
        assert rag.quarantine_blocks({"hash": "abc", "failures": n}, "abc") is False, (
            f"blocked at {n} failures; MAX_FAILURES is {rag.MAX_FAILURES}")
    assert rag.quarantine_blocks(
        {"hash": "abc", "failures": rag.MAX_FAILURES}, "abc") is True


def test_changed_content_clears_the_block(rag):
    """A quarantine entry is bound to the content that failed. Edit the note
    and it earns a fresh attempt even past the failure ceiling."""
    entry = {"hash": "old", "failures": rag.MAX_FAILURES + 5}
    assert rag.quarantine_blocks(entry, "new") is False


def test_no_entry_never_blocks(rag):
    assert rag.quarantine_blocks(None, "abc") is False
    assert rag.quarantine_blocks({}, "abc") is False


def test_missing_failures_key_does_not_block(rag):
    """A malformed entry must fail open, not silently exile a note."""
    assert rag.quarantine_blocks({"hash": "abc"}, "abc") is False


# ---------------------------------------------------------------------------
# wait_for_processing — Open WebUI 0.11.0 made uploads asynchronous.
# ---------------------------------------------------------------------------

def test_wait_polls_until_status_settles(rag, monkeypatch):
    statuses = iter(["pending", "pending", "completed"])
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResponse(payload={"data": {"status": next(statuses)}})

    monkeypatch.setattr(rag.session, "get", fake_get)
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)

    assert rag.wait_for_processing("file-1") == "completed"
    assert len(calls) == 3
    assert "file-1" in calls[0]


def test_wait_returns_immediately_when_already_done(rag, monkeypatch):
    monkeypatch.setattr(rag.session, "get",
                        lambda url, **kw: FakeResponse(
                            payload={"data": {"status": "completed"}}))
    monkeypatch.setattr(rag.time, "sleep",
                        lambda s: pytest.fail("slept despite a settled status"))
    assert rag.wait_for_processing("file-1") == "completed"


def test_wait_times_out_with_the_file_id_in_the_message(rag, monkeypatch):
    """The timeout must name the file and the ceiling — a bare TimeoutError
    in a 2000-file run tells the operator nothing."""
    monkeypatch.setattr(rag.session, "get",
                        lambda url, **kw: FakeResponse(
                            payload={"data": {"status": "pending"}}))
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    clock = iter([0.0] + [float(i) for i in range(1, 10_000)])
    monkeypatch.setattr(rag.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError) as exc:
        rag.wait_for_processing("file-42")
    assert "file-42" in str(exc.value)
    assert str(rag.PROCESSING_TIMEOUT_SECONDS) in str(exc.value)


# ---------------------------------------------------------------------------
# add_to_collection — the two 400s that look identical in a stock traceback.
# ---------------------------------------------------------------------------

def test_duplicate_content_raises_its_own_exception(rag, monkeypatch):
    """Distinguishable from a real failure: the caller treats it as
    already-indexed rather than dropping the note."""
    monkeypatch.setattr(rag.session, "post",
                        lambda url, **kw: FakeResponse(
                            400, '{"detail":"400: Duplicate content detected."}'))
    with pytest.raises(rag.DuplicateContent):
        rag.add_to_collection("file-1")


def test_empty_content_400_keeps_the_server_message(rag, monkeypatch):
    """The other 400. requests' HTTPError carries only the status line, which
    is exactly what made these two indistinguishable in the logs — so the
    body must survive into the exception text."""
    monkeypatch.setattr(rag.session, "post",
                        lambda url, **kw: FakeResponse(
                            400, '{"detail":"400: The content provided is empty."}'))
    with pytest.raises(requests.HTTPError) as exc:
        rag.add_to_collection("file-1")
    assert "The content provided is empty" in str(exc.value)
    assert not isinstance(exc.value, rag.DuplicateContent)


def test_server_error_keeps_the_server_message(rag, monkeypatch):
    monkeypatch.setattr(rag.session, "post",
                        lambda url, **kw: FakeResponse(500, "upstream exploded"))
    with pytest.raises(requests.HTTPError) as exc:
        rag.add_to_collection("file-1")
    assert "upstream exploded" in str(exc.value)


def test_successful_add_raises_nothing(rag, monkeypatch):
    monkeypatch.setattr(rag.session, "post", lambda url, **kw: FakeResponse(200))
    rag.add_to_collection("file-1")


# ---------------------------------------------------------------------------
# push_file — upload, wait, add; clean up the orphan if the add fails.
# ---------------------------------------------------------------------------

def test_push_file_waits_before_adding(rag, monkeypatch, tmp_path):
    """Ordering is the whole point: adding before extraction finishes is the
    400 that started this."""
    order = []
    monkeypatch.setattr(rag, "upload_file", lambda p, r: order.append("upload") or "fid")
    monkeypatch.setattr(rag, "wait_for_processing", lambda f: order.append("wait"))
    monkeypatch.setattr(rag, "add_to_collection", lambda f: order.append("add"))

    note = tmp_path / "n.md"
    note.write_text("body")
    assert rag.push_file(note, "n.md") == "fid"
    assert order == ["upload", "wait", "add"]


def test_push_file_deletes_the_orphan_when_add_fails(rag, monkeypatch, tmp_path):
    """A failed add must not leave an unreferenced file row behind."""
    deleted = []
    monkeypatch.setattr(rag, "upload_file", lambda p, r: "fid")
    monkeypatch.setattr(rag, "wait_for_processing", lambda f: "completed")
    monkeypatch.setattr(rag, "add_to_collection",
                        lambda f: (_ for _ in ()).throw(requests.HTTPError("boom")))
    monkeypatch.setattr(rag, "delete_file", lambda f: deleted.append(f))

    note = tmp_path / "n.md"
    note.write_text("body")
    with pytest.raises(requests.HTTPError):
        rag.push_file(note, "n.md")
    assert deleted == ["fid"]


def test_push_file_propagates_duplicate_content(rag, monkeypatch, tmp_path):
    """DuplicateContent has to survive push_file so the modified path can
    recognise an already-current note instead of counting it as a failure."""
    monkeypatch.setattr(rag, "upload_file", lambda p, r: "fid")
    monkeypatch.setattr(rag, "wait_for_processing", lambda f: "completed")
    monkeypatch.setattr(rag, "add_to_collection",
                        lambda f: (_ for _ in ()).throw(rag.DuplicateContent("dup")))
    monkeypatch.setattr(rag, "delete_file", lambda f: None)

    note = tmp_path / "n.md"
    note.write_text("body")
    with pytest.raises(rag.DuplicateContent):
        rag.push_file(note, "n.md")


# ---------------------------------------------------------------------------
# remove_from_collection — already-gone is success, real errors are not.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [400, 404])
def test_remove_treats_already_gone_as_success(rag, monkeypatch, code):
    monkeypatch.setattr(rag.session, "post", lambda url, **kw: FakeResponse(code))
    rag.remove_from_collection("file-1")


def test_remove_still_raises_on_a_real_error(rag, monkeypatch):
    monkeypatch.setattr(rag.session, "post", lambda url, **kw: FakeResponse(500))
    with pytest.raises(requests.HTTPError):
        rag.remove_from_collection("file-1")
