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


REAL_KEY = b"0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("key", [
    b"", b"   ", b"\n\t",
    # HMAC zero-pads short keys, so every one of these computes the MAC of the
    # empty key. A NUL-only key file survived the first version of the check.
    b"\x00", b"\x00" * 32, b"\x00" * 64, b"\n\x00\x00\n",
    b"short",                      # guessable, whatever it is
])
@pytest.mark.parametrize("required", [True, False])
def test_forgery_under_an_empty_key_is_refused(key: bytes, required: bool) -> None:
    forged = hmac.new(b"", DATA, hashlib.sha256).hexdigest() if not key.strip(b" \n\t\x00") \
        else hmac.new(key, DATA, hashlib.sha256).hexdigest()
    with pytest.raises(hs.HandoffError, match="shorter than 16 significant bytes"):
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
    sig = hmac.new(REAL_KEY, DATA, hashlib.sha256).hexdigest()
    hs.verify_signature(DATA, sig, REAL_KEY, required=True)


@pytest.mark.parametrize("make", ["missing", "directory", "dangling"])
def test_configured_key_file_that_is_not_a_file_fails_closed(
        tmp_path, monkeypatch: pytest.MonkeyPatch, make: str) -> None:
    """A moved or deleted key file used to warn and fall back to "no key",
    which with the default HANDOFF_REQUIRE_SIGNATURE off accepted unsigned
    handoffs: authenticity switched off silently."""
    kf = tmp_path / "key"
    if make == "directory":
        kf.mkdir()
    elif make == "dangling":
        kf.symlink_to(tmp_path / "nowhere")
    monkeypatch.setenv("HANDOFF_HMAC_KEY_FILE", str(kf))
    monkeypatch.delenv("HANDOFF_HMAC_KEY", raising=False)
    with pytest.raises(hs.HandoffError, match="not a readable file"):
        hs.load_hmac_key()
