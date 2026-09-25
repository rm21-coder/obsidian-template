"""
test_handoff_blob_pull.py -- remote blob names and sizes are not trusted.

The Azure Blob relay was dropped from the maintainer's own install in August
but still ships as an optional secondary producer, so it is held to the same
standard. Microsoft M-DASH (2026-09-23) validated five findings here: blob
names reaching local paths (CWE-22 x2) and the SAS request URL (CWE-116)
unchecked, and responses buffered whole with no ceiling (CWE-770 x2).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import handoff_blob_pull as hbp


class _Raw:
    def __init__(self, body: bytes): self._b = body
    def stream(self, n, decode_content=True):
        assert decode_content is False, "bytes must be counted on the wire, undecoded"
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]


class _Resp:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.raw = _Raw(body); self.status_code = 200; self.headers = headers or {}
    def raise_for_status(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Session:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body; self.headers = headers; self.urls = []
    def get(self, url, timeout=None, stream=False):
        assert stream, "responses must be streamed, not buffered whole"
        self.urls.append(url); return _Resp(self.body, self.headers)


class TestBlobNamesCannotChooseAPath:

    @pytest.mark.parametrize("name", [
        "../../evil.json", "/abs/evil.json", "..\\evil.json",
        "x?sig=forged.ready", "a#frag.json", ".hidden.json", "a/b.json",
    ])
    def test_unsafe_names_are_dropped(self, name: str) -> None:
        assert hbp.group_by_handoff_id([name]) == {}

    def test_legitimate_set_is_grouped(self) -> None:
        g = hbp.group_by_handoff_id([
            "schedule-handoff-2026-08-05.v1.json",
            "schedule-handoff-2026-08-05.v1.ready"])
        assert g == {"schedule-handoff-2026-08-05.v1": {".json", ".ready"}}

    def test_download_refuses_a_destination_outside_local_dir(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hbp, "LOCAL_DIR", tmp_path / "in")
        (tmp_path / "in").mkdir()
        with pytest.raises(ValueError):
            hbp.download_blob(_Session(b"x"), "n", tmp_path / "out.json")
        assert not (tmp_path / "out.json").exists()

    def test_name_is_quoted_in_the_sas_url(self) -> None:
        path = hbp._blob_url("a?b#c").split("?")[0]
        assert "?" not in path.rsplit("/", 1)[-1] and "#" not in path


class TestResponsesAreBounded:

    def test_oversized_blob_is_refused_and_not_written(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hbp, "LOCAL_DIR", tmp_path)
        big = _Session(b"x" * (hbp.MAX_BLOB_BYTES + 1))
        with pytest.raises(ValueError):
            hbp.download_blob(big, "n", tmp_path / "n.json")
        assert not (tmp_path / "n.json").exists()
        assert not (tmp_path / "n.json.part").exists()

    def test_oversized_listing_is_refused(self) -> None:
        with pytest.raises(ValueError):
            hbp.list_blob_names(_Session(b"<a>" + b"x" * hbp.MAX_LISTING_BYTES + b"</a>"))

    def test_normal_blob_lands(self, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hbp, "LOCAL_DIR", tmp_path)
        hbp.download_blob(_Session(b'{"ok": 1}'), "n", tmp_path / "n.json")
        assert (tmp_path / "n.json").read_bytes() == b'{"ok": 1}'


class TestAdversarialReviewRound2:
    """Second, adversarial pass over the M-DASH fixes (2026-09-25)."""

    def test_declared_content_encoding_is_refused(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # requests would inflate this transparently; counting after that let
        # a 26 KB double-gzip blob reach ~1 GiB in memory.
        monkeypatch.setattr(hbp, "LOCAL_DIR", tmp_path)
        s = _Session(b"\x1f\x8b tiny", headers={"Content-Encoding": "gzip, gzip"})
        with pytest.raises(hbp.BlobRefused, match="Content-Encoding"):
            hbp.download_blob(s, "n.json", tmp_path / "n.json")
        assert list(tmp_path.iterdir()) == []

    def test_one_refused_set_does_not_stop_the_sets_after_it(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hbp, "LOCAL_DIR", tmp_path)
        for k, v in (("ACCOUNT_URL", "https://a"), ("CONTAINER", "c"), ("SAS", "s")):
            monkeypatch.setattr(hbp, k, v)
        monkeypatch.setattr(hbp, "list_blob_names", lambda s: [
            "0000.json", "0000.ready", "good.json", "good.ready"])
        monkeypatch.setattr(hbp, "delete_blob", lambda *a, **k: None)
        landed = []
        def fake_download(session, name, dest):
            if name.startswith("0000"):
                raise hbp.BlobRefused("response exceeds 16777216 bytes; refused")
            landed.append(name)
        monkeypatch.setattr(hbp, "download_blob", fake_download)
        monkeypatch.setattr(hbp.requests, "Session", lambda: object())
        assert hbp.run(dry_run=False) == 1, "a refusal must surface as a failed run"
        assert landed == ["good.json", "good.ready"]

    def test_unparseable_listing_fails_the_run_instead_of_crashing(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in (("ACCOUNT_URL", "https://a"), ("CONTAINER", "c"), ("SAS", "s")):
            monkeypatch.setattr(hbp, k, v)
        monkeypatch.setattr(hbp.requests, "Session", lambda: _Session(b"<not xml"))
        assert hbp.run(dry_run=False) == 1

    def test_planted_part_symlink_is_not_followed(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        inbox = tmp_path / "in"; inbox.mkdir()
        victim = tmp_path / "victim.txt"; victim.write_text("original")
        monkeypatch.setattr(hbp, "LOCAL_DIR", inbox)
        (inbox / "n.json.part").symlink_to(victim)
        hbp.download_blob(_Session(b"PWN"), "n.json", inbox / "n.json")
        assert victim.read_text() == "original"
        assert (inbox / "n.json").read_bytes() == b"PWN"


def test_listing_with_entity_declarations_is_refused() -> None:
    """defusedxml (B314 closed 2026-09-25): Azure never declares entities."""
    body = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            b'<EnumerationResults><Blobs><Blob><Name>&a;</Name></Blob></Blobs></EnumerationResults>')
    with pytest.raises(hbp.BlobRefused, match="declares entities"):
        hbp.list_blob_names(_Session(body))


def test_ordinary_listing_still_parses() -> None:
    body = (b'<?xml version="1.0" encoding="utf-8"?><EnumerationResults><Blobs>'
            b'<Blob><Name>schedule-handoff-2026-09-25.v1.json</Name></Blob></Blobs></EnumerationResults>')
    assert hbp.list_blob_names(_Session(body)) == ["schedule-handoff-2026-09-25.v1.json"]
