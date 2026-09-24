"""
test_handoff_source.py -- an empty HMAC key must not verify anything.

An empty or whitespace-only HANDOFF_HMAC_KEY_FILE loaded as b"" rather than
None, so verify_signature went ahead under a key anyone can compute and every
forged handoff verified. Microsoft M-DASH 2026-09-23 (CWE-347).
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

import handoff_source as hs

DATA = b'{"meetings": []}'


@pytest.mark.parametrize("key", [b"", b"   ", b"\n\t"])
@pytest.mark.parametrize("required", [True, False])
def test_forgery_under_an_empty_key_is_refused(key: bytes, required: bool) -> None:
    forged = hmac.new(key, DATA, hashlib.sha256).hexdigest()
    with pytest.raises(hs.HandoffError, match="empty"):
        hs.verify_signature(DATA, forged, key, required=required)


def test_empty_key_file_is_refused_end_to_end(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    kf = tmp_path / "key"
    kf.write_bytes(b"  \n")
    monkeypatch.setenv("HANDOFF_HMAC_KEY_FILE", str(kf))
    monkeypatch.delenv("HANDOFF_HMAC_KEY", raising=False)
    key = hs.load_hmac_key()
    forged = hmac.new(key, DATA, hashlib.sha256).hexdigest()
    with pytest.raises(hs.HandoffError):
        hs.verify_signature(DATA, forged, key, required=False)


def test_a_real_key_still_verifies() -> None:
    sig = hmac.new(b"real-key", DATA, hashlib.sha256).hexdigest()
    hs.verify_signature(DATA, sig, b"real-key", required=True)
