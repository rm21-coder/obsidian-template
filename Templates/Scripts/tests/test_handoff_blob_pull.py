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


class _Resp:
    def __init__(self, body: bytes): self._b = body; self.status_code = 200
    def raise_for_status(self): pass
    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Session:
    def __init__(self, body: bytes): self.body = body; self.urls = []
    def get(self, url, timeout=None, stream=False):
        assert stream, "responses must be streamed, not buffered whole"
        self.urls.append(url); return _Resp(self.body)


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
