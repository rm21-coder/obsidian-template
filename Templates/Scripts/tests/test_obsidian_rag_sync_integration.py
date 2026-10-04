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


NOT_FOUND = '{"detail":"We could not find what you\'re looking for :/"}'


class FakeWebUI:
    def __init__(self, collection_id: str, *, pending_polls: int = 0,
                 fail_adds: bool = False, fail_purges: bool = False):
        self.collection_id = collection_id
        self.pending_polls = pending_polls
        self.fail_adds = fail_adds
        # Models the container being down for removals: the transport
        # raises, as requests does when nothing is listening.
        self.fail_purges = fail_purges
        # A server-side fault answered as a bare 400, which is what Open
        # WebUI does for many errors; must never read as "already gone".
        self.remove_status: int | None = None
        self.delete_status: int | None = None
        # Every purge call answered 404 by something that is not Open WebUI's
        # handler: a proxy, or an upgrade that moved the routes.
        self.routes_moved = False
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
            if self.fail_purges:
                import requests
                raise requests.ConnectionError("injected: webui down")
            if self.routes_moved:
                return FakeResponse(404, '{"detail":"Not Found"}')
            if self.remove_status is not None:
                return FakeResponse(self.remove_status, '{"detail":"injected remove fault"}')
            fid = kw["json"]["file_id"]
            if fid not in self.collection:
                # What v0.11.3 really answers for a file not in the collection.
                return FakeResponse(400, NOT_FOUND)
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
        if self.fail_purges:
            import requests
            raise requests.ConnectionError("injected: webui down")
        if self.routes_moved:
            return FakeResponse(404, '{"detail":"Not Found"}')
        if self.delete_status is not None:
            return FakeResponse(self.delete_status, '{"detail":"Error deleting files"}')
        fid = url.rsplit("/", 1)[-1]
        if fid not in self.files:
            return FakeResponse(404, NOT_FOUND)
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
    # Windows derives Path.home() from USERPROFILE, not HOME, and the module
    # puts its state under %LOCALAPPDATA% there rather than ~/.local/share.
    # Redirect all three or the guard below trips on Windows and every test
    # in this file errors out rather than running.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
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


# ---------------------------------------------------------------------------
# A classification deindex whose purge fails must not be forgotten (#320).
# ---------------------------------------------------------------------------

RESTRICTED = "---\nclassification: restricted\n---\n" + BODY


def _latest_report(sync) -> str:
    reports = sorted((sync.vault / "Creations").glob("RAG-Sync-*.md"))
    assert reports, "no run report written"
    return reports[-1].read_text()


def test_failed_deindex_keeps_the_copy_pending_and_reports_it(sync):
    """Open WebUI down while a note is raised to restricted. The old code
    popped the state entry anyway and reported the note as deindexed, so the
    restricted copy stayed searchable and no later run tried again."""
    sync.note("Meetings/raised.md", body=RESTRICTED)
    server = FakeWebUI("test-collection")
    fid = server.seed("the note while it was still internal")
    sync.state({"Meetings/raised.md": {"hash": "h", "file_id": fid}})

    server.fail_purges = True
    rc = sync.run(server)

    assert rc == 2, "a failed purge must be reported as an error"
    pending = sync.read_state()["pending_purge"]
    assert pending[fid]["path"] == "Meetings/raised.md"
    assert "injected: webui down" in pending[fid]["last_error"]
    report = _latest_report(sync)
    assert "sync_status: FAIL" in report
    assert "### Purge failures (still in the index, retried next run)" in report
    assert "still in the index, purge pending" in report
    deindexed = report.split("### Deindexed (classification)")[1].split("##")[0]
    assert "Meetings/raised.md" not in deindexed, (
        "a note whose purge failed was reported as deindexed")


def test_pending_purge_is_retried_until_it_succeeds(sync):
    sync.note("Meetings/raised.md", body=RESTRICTED)
    server = FakeWebUI("test-collection")
    fid = server.seed("the note while it was still internal")
    sync.state({"Meetings/raised.md": {"hash": "h", "file_id": fid}})

    server.fail_purges = True
    assert sync.run(server) == 2
    assert sync.run(server) == 2, "a still-failing purge stays an error"
    assert sync.read_state()["pending_purge"][fid]["attempts"] == 2
    assert fid in server.collection

    server.fail_purges = False
    assert sync.run(server) == 0
    assert fid not in server.collection, "restricted copy left in the index"
    assert sync.read_state()["pending_purge"] == {}
    assert "### Pending purges completed" in _latest_report(sync)


def test_successful_deindex_is_reported_and_leaves_nothing_pending(sync):
    sync.note("Meetings/raised.md", body=RESTRICTED)
    server = FakeWebUI("test-collection")
    fid = server.seed("the note while it was still internal")
    sync.state({"Meetings/raised.md": {"hash": "h", "file_id": fid}})

    assert sync.run(server) == 0
    assert fid not in server.collection
    assert sync.read_state()["pending_purge"] == {}
    report = _latest_report(sync)
    assert "sync_status: PASS" in report
    deindexed = report.split("### Deindexed (classification)")[1].split("##")[0]
    assert "Meetings/raised.md" in deindexed


# ---------------------------------------------------------------------------
# Review follow-ups: superseded copies, partial purges, and what "gone" means.
# ---------------------------------------------------------------------------

def test_failed_purge_of_a_superseded_copy_goes_pending(sync):
    """The update path used to log a failed purge of the pre-edit copy and
    overwrite old_id, so text an edit had removed stayed indexed forever."""
    sync.note("Meetings/edited.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed("pre-edit text the user later redacted")
    sync.state({"Meetings/edited.md": {"hash": "stale", "file_id": old_id}})

    server.remove_status = 500
    assert sync.run(server) == 2
    pending = sync.read_state()["pending_purge"]
    assert pending[old_id]["kind"] == "superseded copy"
    assert pending[old_id]["path"] == "Meetings/edited.md"
    assert BODY in server.contents(), "the new copy should still be indexed"
    assert "still in the index, purge pending (superseded copy)" in _latest_report(sync)

    server.remove_status = None
    assert sync.run(server) == 0
    assert old_id not in server.files
    assert sync.read_state()["pending_purge"] == {}


def test_a_failed_remove_still_attempts_the_file_delete(sync):
    sync.note("Meetings/edited.md")
    server = FakeWebUI("test-collection")
    old_id = server.seed("pre-edit text")
    sync.state({"Meetings/edited.md": {"hash": "stale", "file_id": old_id}})

    server.remove_status = 500
    sync.run(server)
    assert f"DELETE http://webui.invalid/api/v1/files/{old_id}" in server.calls
    assert old_id not in server.files


@pytest.mark.parametrize("which", ["remove_status", "delete_status"])
def test_a_bare_400_is_a_failure_not_already_gone(sync, which):
    """Open WebUI answers server faults with 400s; only its not-found
    message (or a 404) means the copy is gone."""
    sync.note("Meetings/raised.md", body=RESTRICTED)
    server = FakeWebUI("test-collection")
    fid = server.seed("the note while it was still internal")
    sync.state({"Meetings/raised.md": {"hash": "h", "file_id": fid}})

    setattr(server, which, 400)
    assert sync.run(server) == 2
    assert "400 from" in sync.read_state()["pending_purge"][fid]["last_error"]


def test_an_already_purged_copy_clears_from_pending(sync):
    """Retrying a purge the server already finished (400 not-found from
    remove, 404 from delete) clears the entry instead of failing forever."""
    server = FakeWebUI("test-collection")
    sync.state({})
    state = sync.read_state()
    state["pending_purge"] = {"file-gone": {"path": "Meetings/x.md",
                                            "kind": "deleted", "attempts": 1}}
    sync.module.STATE_FILE.write_text(json.dumps(state))

    assert sync.run(server) == 0
    assert sync.read_state()["pending_purge"] == {}


def test_moved_routes_never_count_as_gone(sync):
    """Round 2: a bare 404 was "already gone". Behind a proxy or after an
    upgrade that moves the API, every purge 404s and a deindex-only run
    reported restricted copies as deindexed while they stayed indexed."""
    sync.note("Meetings/raised.md", body=RESTRICTED)
    server = FakeWebUI("test-collection")
    fid = server.seed("the note while it was still internal")
    sync.state({"Meetings/raised.md": {"hash": "h", "file_id": fid}})

    server.routes_moved = True
    assert sync.run(server) == 2
    pending = sync.read_state()["pending_purge"]
    assert '404 from /files/{id}: {"detail":"Not Found"}' in pending[fid]["last_error"]
    deindexed = _latest_report(sync).split(
        "### Deindexed (classification)")[1].split("##")[0]
    assert "Meetings/raised.md" not in deindexed


def _pending(n: int) -> dict:
    return {f"file-{i}": {"path": f"Meetings/n{i}.md", "kind": "deleted",
                          "attempts": 1, "last_error": "down",
                          "first_failed": f"2026-10-0{i}T00:00:00"}
            for i in range(1, n + 1)}


def test_pending_purge_retries_are_capped_per_run_oldest_first(sync, monkeypatch):
    """Each retry can cost two 60s requests before any normal work, so an
    outage's backlog must not stall every later run."""
    monkeypatch.setattr(sync.module, "MAX_PURGE_RETRIES_PER_RUN", 2, raising=False)
    server = FakeWebUI("test-collection")
    sync.state({})
    state = sync.read_state()
    state["pending_purge"] = _pending(3)
    sync.module.STATE_FILE.write_text(json.dumps(state))

    assert sync.run(server) == 2, "entries left waiting are still errors"
    assert sum(c.startswith("DELETE") for c in server.calls) == 2
    assert list(sync.read_state()["pending_purge"]) == ["file-3"], "oldest first"
    report = _latest_report(sync)
    assert "1 pending purge(s) not retried this run (cap 2); still in the index" in report
    assert "not retried this run (cap 2); last error: down" in report

    assert sync.run(server) == 0
    assert sync.read_state()["pending_purge"] == {}
