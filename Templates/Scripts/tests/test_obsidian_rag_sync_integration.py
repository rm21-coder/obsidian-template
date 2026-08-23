"""Integration cover for obsidian-rag-sync.py main().

The unit tests next door pin the helpers. This file drives the whole sync
loop against an in-memory stand-in for Open WebUI, because the defect that
actually cost notes lived in main()'s *ordering*, not in any one function:
the modified path removed a note from the collection before pushing its
replacement, so every failed add silently deleted a note that was fine.
No helper is wrong in that story. Only the sequence is.

FakeWebUI models the parts of the 0.11.0 API that made the ordering matter:

  - uploads are asynchronous. POST /api/v1/files/ returns immediately with
    data.status == "pending"; the text is only extracted after
    `pending_polls` status checks.
  - adding a file whose text has not been extracted yet fails 400 with
    "The content provided is empty".
  - adding content byte-identical to something already in the collection
    fails 400 with "Duplicate content detected".

`fail_adds` forces every add to fail, which is the lever the regression
tests pull: whatever else happens, a note that was in the collection before
the run must still be in it afterwards.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fake Open WebUI.
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"{self.status_code} error")


class FakeWebUI:
    def __init__(self, collection_id: str, *, pending_polls: int = 0,
                 fail_adds: bool = False):
        self.collection_id = collection_id
        self.pending_polls = pending_polls
        self.fail_adds = fail_adds
        self.files: dict[str, dict] = {}      # file_id -> {content, polls}
        self.collection: set[str] = set()     # file_ids in the collection
        self._next = 0
        self.calls: list[str] = []

    # -- helpers ---------------------------------------------------------
    def seed(self, content: str) -> str:
        """Put a file straight into the collection, as a prior sync would."""
        fid = self._new_id()
        self.files[fid] = {"content": content, "polls": 10_000}
        self.collection.add(fid)
        return fid

    def _new_id(self) -> str:
        self._next += 1
        return f"file-{self._next}"

    def _extracted(self, fid: str) -> bool:
        return self.files[fid]["polls"] >= self.pending_polls

    def contents(self) -> set[str]:
        return {self.files[f]["content"] for f in self.collection}

    # -- transport -------------------------------------------------------
    def post(self, url, **kw):
        self.calls.append(f"POST {url}")
        if url.endswith("/api/v1/files/"):
            fid = self._new_id()
            content = kw["files"]["file"][1].read().decode("utf-8", "replace")
            self.files[fid] = {"content": content, "polls": 0}
            return FakeResponse(200, payload={"id": fid,
                                              "data": {"status": "pending"}})
        if url.endswith("/file/add"):
            fid = kw["json"]["file_id"]
            if self.fail_adds:
                return FakeResponse(500, "injected add failure")
            if not self._extracted(fid):
                return FakeResponse(
                    400, '{"detail":"400: The content provided is empty."}')
            mine = self.files[fid]["content"]
            if any(self.files[o]["content"] == mine for o in self.collection
                   if o != fid):
                return FakeResponse(
                    400, '{"detail":"400: Duplicate content detected."}')
            self.collection.add(fid)
            return FakeResponse(200)
        if url.endswith("/file/remove"):
            fid = kw["json"]["file_id"]
            if fid not in self.collection:
                return FakeResponse(400, "not in collection")
            self.collection.discard(fid)
            return FakeResponse(200)
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kw):
        self.calls.append(f"GET {url}")
        fid = url.rsplit("/", 1)[-1]
        rec = self.files[fid]
        rec["polls"] += 1
        status = "completed" if self._extracted(fid) else "pending"
        return FakeResponse(200, payload={"data": {"status": status}})

    def delete(self, url, **kw):
        self.calls.append(f"DELETE {url}")
        fid = url.rsplit("/", 1)[-1]
        self.collection.discard(fid)
        self.files.pop(fid, None)
        return FakeResponse(200)


# ---------------------------------------------------------------------------
# Fixture.
# ---------------------------------------------------------------------------

BODY = "Hopkins IT strategy discussion. " * 10   # clears MIN_BODY_CHARS


@pytest.fixture
def sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts_dir: Path):
    """Import the module against a throwaway HOME/vault and hand back a
    driver that wires a FakeWebUI in and runs main()."""
    home = tmp_path / "home"
    vault = home / "Obsidian"
    (vault / "Creations").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OBSIDIAN_VAULT", str(vault))
    monkeypatch.setenv("OPEN_WEBUI_URL", "http://webui.invalid")
    monkeypatch.setenv("OBSIDIAN_COLLECTION_ID", "test-collection")

    import secret_store
    monkeypatch.setattr(secret_store, "get_secret", lambda name: "test-key")

    path = scripts_dir / "obsidian-rag-sync.py"
    spec = importlib.util.spec_from_file_location("obsidian_rag_sync_it", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["obsidian_rag_sync_it"] = mod
    spec.loader.exec_module(mod)
    assert Path(mod.STATE_DIR).is_relative_to(home)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    vault_dir = vault

    class Driver:
        module = mod
        vault = vault_dir

        def note(self, rel: str, body: str = BODY) -> Path:
            p = vault_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
            return p

        def state(self, files: dict, quarantine: dict | None = None) -> None:
            mod.STATE_FILE.write_text(json.dumps(
                {"files": files, "quarantine": quarantine or {}}))

        def read_state(self) -> dict:
            return json.loads(mod.STATE_FILE.read_text())

        def run(self, server: FakeWebUI, *argv: str) -> int:
            monkeypatch.setattr(mod.session, "post", server.post)
            monkeypatch.setattr(mod.session, "get", server.get)
            monkeypatch.setattr(mod.session, "delete", server.delete)
            monkeypatch.setattr(sys, "argv", ["obsidian-rag-sync.py", *argv])
            return mod.main()

    yield Driver()
    sys.modules.pop("obsidian_rag_sync_it", None)


def hash_of(mod, path: Path) -> str:
    return mod.file_hash(path)


# ---------------------------------------------------------------------------
# THE regression: a failed add must never cost an already-indexed note.
# ---------------------------------------------------------------------------

def test_failed_add_leaves_the_existing_note_indexed(sync):
    """The defect that removed 318 notes. The old ordering removed first, so
    an add that failed for any reason left nothing behind."""
    note = sync.note("Meetings/one.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed("stale content")
    sync.state({"Meetings/one.md": {"hash": "stale-hash", "file_id": old_id}})

    server.fail_adds = True
    rc = sync.run(server)

    assert rc != 0, "a failed add must be reported as an error"
    assert old_id in server.collection, (
        "the previously-indexed note was dropped from the collection by a "
        "failed update — this is the regression")


def test_failed_add_keeps_the_old_file_id_in_state(sync):
    """State must not advance past a failure, or the next run sees the note
    as unchanged and never retries it."""
    sync.note("Meetings/one.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed("stale content")
    sync.state({"Meetings/one.md": {"hash": "stale-hash", "file_id": old_id}})

    server.fail_adds = True
    sync.run(server)

    assert sync.read_state()["files"]["Meetings/one.md"]["file_id"] == old_id


def test_failed_add_records_a_quarantine_failure(sync):
    sync.note("Meetings/one.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed("stale content")
    sync.state({"Meetings/one.md": {"hash": "stale-hash", "file_id": old_id}})

    server.fail_adds = True
    sync.run(server)

    q = sync.read_state()["quarantine"]["Meetings/one.md"]
    assert q["failures"] == 1
    assert "injected add failure" in q["last_error"], (
        "the server's message must reach the quarantine record, or the next "
        "operator cannot tell these failures apart")


# ---------------------------------------------------------------------------
# Async uploads: the run must survive an extraction queue that lags.
# ---------------------------------------------------------------------------

def test_slow_extraction_still_indexes_the_note(sync):
    """Three polls of "pending" before the text lands. The pre-fix code added
    immediately and took a 400."""
    sync.note("Knowledge/new.md")
    server = FakeWebUI("test-collection", pending_polls=3)

    rc = sync.run(server)

    assert rc == 0
    assert BODY in server.contents()


# ---------------------------------------------------------------------------
# Duplicate content: stale local state, healthy index.
# ---------------------------------------------------------------------------

def test_duplicate_content_is_not_an_error_and_keeps_the_note(sync):
    """With add-then-remove the old copy is still present, so re-pushing
    identical content is rejected. That means the index is already right."""
    note = sync.note("Meetings/dup.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed(BODY)                      # same body as on disk
    sync.state({"Meetings/dup.md": {"hash": "stale-hash", "file_id": old_id}})

    rc = sync.run(server)

    assert rc == 0, "a duplicate means already-indexed, not a failure"
    assert old_id in server.collection
    assert "Meetings/dup.md" not in sync.read_state()["quarantine"]


def test_duplicate_content_refreshes_the_stored_hash(sync):
    """Otherwise the note is re-attempted on every single run, forever."""
    note = sync.note("Meetings/dup.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed(BODY)
    sync.state({"Meetings/dup.md": {"hash": "stale-hash", "file_id": old_id}})

    sync.run(server)

    stored = sync.read_state()["files"]["Meetings/dup.md"]
    assert stored["hash"] == hash_of(sync.module, note)
    assert stored["file_id"] == old_id


# ---------------------------------------------------------------------------
# The ordinary paths still work.
# ---------------------------------------------------------------------------

def test_new_note_is_added(sync):
    sync.note("Knowledge/fresh.md")
    server = FakeWebUI("test-collection")

    assert sync.run(server) == 0
    assert BODY in server.contents()
    assert "Knowledge/fresh.md" in sync.read_state()["files"]


def test_successful_update_purges_the_old_copy(sync):
    """Add-then-remove must still remove. Leaving both would double-index."""
    sync.note("Meetings/one.md", body="Updated body. " * 20)
    server = FakeWebUI("test-collection")
    old_id = server.seed("previous content")
    sync.state({"Meetings/one.md": {"hash": "stale-hash", "file_id": old_id}})

    assert sync.run(server) == 0
    assert old_id not in server.collection, "old copy left behind"
    assert old_id not in server.files, "old file row left behind"
    assert len(server.collection) == 1


def test_deleted_note_is_removed_from_the_collection(sync):
    server = FakeWebUI("test-collection")
    gone_id = server.seed("content of a note since deleted")
    sync.state({"Meetings/gone.md": {"hash": "h", "file_id": gone_id}})

    assert sync.run(server) == 0
    assert gone_id not in server.collection


def test_dry_run_touches_nothing(sync):
    sync.note("Knowledge/fresh.md")
    server = FakeWebUI("test-collection")

    assert sync.run(server, "--dry-run") == 0
    assert server.collection == set()
    assert not any("file/add" in c for c in server.calls)


# ---------------------------------------------------------------------------
# Quarantine, end to end.
# ---------------------------------------------------------------------------

def test_note_at_max_failures_is_skipped(sync):
    note = sync.note("Knowledge/bad.md")
    server = FakeWebUI("test-collection")
    sync.state({}, quarantine={"Knowledge/bad.md": {
        "hash": hash_of(sync.module, note),
        "failures": sync.module.MAX_FAILURES,
        "last_error": "old", "last_attempt": "2026-01-01T00:00:00"}})

    assert sync.run(server) == 0
    assert server.collection == set(), "quarantined note should not be pushed"


def test_note_below_max_failures_is_retried(sync):
    """The regression, seen from main(): one past failure must not exile a
    note from every future run."""
    note = sync.note("Knowledge/flaky.md")
    server = FakeWebUI("test-collection")
    sync.state({}, quarantine={"Knowledge/flaky.md": {
        "hash": hash_of(sync.module, note),
        "failures": 1,
        "last_error": "a blip", "last_attempt": "2026-01-01T00:00:00"}})

    assert sync.run(server) == 0
    assert BODY in server.contents(), (
        "a note with a single prior failure was skipped instead of retried")


def test_reset_quarantine_retries_everything(sync):
    note = sync.note("Knowledge/bad.md")
    server = FakeWebUI("test-collection")
    sync.state({}, quarantine={"Knowledge/bad.md": {
        "hash": hash_of(sync.module, note),
        "failures": sync.module.MAX_FAILURES,
        "last_error": "old", "last_attempt": "2026-01-01T00:00:00"}})

    assert sync.run(server, "--reset-quarantine") == 0
    assert BODY in server.contents()


# ---------------------------------------------------------------------------
# Classification gating still holds through main().
# ---------------------------------------------------------------------------

def test_restricted_note_is_never_uploaded(sync):
    sync.note("Meetings/secret.md",
              body="---\nclassification: restricted\n---\n" + BODY)
    server = FakeWebUI("test-collection")

    assert sync.run(server) == 0
    assert server.collection == set()
    assert not any("/api/v1/files/" in c and c.startswith("POST")
                   for c in server.calls), "restricted content left the machine"


def test_unknown_classification_is_blocked_fail_secure(sync):
    sync.note("Meetings/odd.md",
              body="---\nclassification: totally-made-up\n---\n" + BODY)
    server = FakeWebUI("test-collection")

    assert sync.run(server) == 0
    assert server.collection == set()
