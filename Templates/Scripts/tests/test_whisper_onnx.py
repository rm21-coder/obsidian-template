"""
test_whisper_onnx.py — the torch-free ONNX Whisper backend.

These tests deliberately avoid loading a model: the ONNX exports are ~1 GB and
downloading one in a unit test would make the suite network-dependent. What is
covered here is everything that goes wrong *silently* -- the parts where a bug
produces fluent, plausible, wrong output instead of an exception. Each of the
first three classes pins a bug that actually occurred while building this.

The one thing not asserted here is end-to-end transcription accuracy, which
needs a real model and real audio. That was validated by hand against
synthesised speech (word-exact with whisper-small) and against openai/whisper's
own reference mel filterbank, which this module's numpy implementation
reproduces bit-for-bit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

import whisper_onnx as wo


# ---------------------------------------------------------------------------
# Mel filterbank.
#
# The highest-risk code in the module: a wrong filterbank does not raise, it
# feeds the encoder a spectrogram it was never trained on, and the model
# responds with confident nonsense. The properties below pin the two ways it
# realistically goes wrong -- the mel scale (Slaney vs HTK) and the
# normalisation (Slaney area vs unit peak).
# ---------------------------------------------------------------------------

class TestMelFilterbank:

    def test_shape_matches_rfft_bins(self) -> None:
        fb = wo.mel_filterbank(wo.SAMPLE_RATE, wo.N_FFT, 80)
        assert fb.shape == (80, wo.N_FFT // 2 + 1)

    def test_supports_128_bin_models(self) -> None:
        """large-v3 and turbo use 128 mel bins where earlier models use 80."""
        assert wo.mel_filterbank(wo.SAMPLE_RATE, wo.N_FFT, 128).shape == (128, 201)

    def test_weights_are_non_negative(self) -> None:
        assert (wo.mel_filterbank(wo.SAMPLE_RATE, wo.N_FFT, 80) >= 0).all()

    def test_uses_the_slaney_mel_scale_not_htk(self) -> None:
        """The scales agree nowhere except by construction at the breakpoint.

        Slaney is linear below 1000 Hz at 200/3 Hz per mel, putting 1000 Hz at
        exactly 15.0 mel. HTK's formula gives ~999.99 there. Whisper's shipped
        filterbank is Slaney; picking HTK yields a subtly warped spectrogram.
        """
        assert wo._hz_to_mel(1000.0) == pytest.approx(15.0, abs=1e-9)
        assert wo._hz_to_mel(0.0) == pytest.approx(0.0, abs=1e-12)
        # Linear region: doubling the frequency doubles the mel value.
        assert wo._hz_to_mel(500.0) == pytest.approx(7.5, abs=1e-9)

    def test_mel_hz_roundtrip(self) -> None:
        hz = np.array([0.0, 100.0, 999.0, 1000.0, 1001.0, 4000.0, 8000.0])
        assert wo._mel_to_hz(wo._hz_to_mel(hz)) == pytest.approx(hz, abs=1e-6)

    def test_filters_are_area_normalised(self) -> None:
        """Slaney normalisation equalises filter *area*, not peak height. With
        unit-peak normalisation the areas would grow with centre frequency."""
        fb = wo.mel_filterbank(wo.SAMPLE_RATE, wo.N_FFT, 80)
        areas = fb.sum(axis=1)
        assert areas.min() > 0
        # Areas stay within a tight band; unnormalised filters span ~20x.
        assert areas.max() / areas.min() < 3.0

    def test_centre_frequencies_increase(self) -> None:
        fb = wo.mel_filterbank(wo.SAMPLE_RATE, wo.N_FFT, 80)
        peaks = fb.argmax(axis=1)
        assert (np.diff(peaks) >= 0).all()


# ---------------------------------------------------------------------------
# Padding. The bug: padding in feature space instead of the time domain.
# ---------------------------------------------------------------------------

class TestPadding:

    def test_pads_short_audio_to_a_full_window(self) -> None:
        assert len(wo.pad_or_trim_audio(np.zeros(1000, np.float32))) == wo.CHUNK_SAMPLES

    def test_trims_long_audio_to_a_full_window(self) -> None:
        long = np.zeros(wo.CHUNK_SAMPLES * 2, np.float32)
        assert len(wo.pad_or_trim_audio(long)) == wo.CHUNK_SAMPLES

    def test_exact_length_is_untouched(self) -> None:
        exact = np.ones(wo.CHUNK_SAMPLES, np.float32)
        assert wo.pad_or_trim_audio(exact) is exact

    def test_time_domain_padding_produces_silence_not_zero_features(self) -> None:
        """The heart of the bug. Log-mel of digital silence is NOT 0.0, so
        zero-filling the feature matrix creates a block unlike anything the
        encoder saw in training -- and the model then stops early instead of
        erroring. Padding in the time domain gives the encoder real silence.
        """
        speech = np.sin(np.linspace(0, 400 * np.pi, 16000)).astype(np.float32)
        padded = wo.pad_or_trim_audio(speech)
        feats = wo.log_mel_spectrogram(padded, 80)
        silent_region = feats[:, 500:]
        assert not np.allclose(silent_region, 0.0), (
            "log-mel of silence should not be zero; padding in feature space "
            "would make it so")

    def test_features_come_out_at_the_expected_frame_count(self) -> None:
        feats = wo.log_mel_spectrogram(
            wo.pad_or_trim_audio(np.zeros(16000, np.float32)), 80)
        assert feats.shape == (80, wo.N_FRAMES)


# ---------------------------------------------------------------------------
# Decoder prompt. The bug: lang_to_id and task_to_id use different key
# spellings, so assuming one silently dropped the task token.
# ---------------------------------------------------------------------------

class _FakeModel:
    """Just enough of WhisperOnnx to exercise prompt construction without
    loading ~1 GB of ONNX."""

    def __init__(self, lang_to_id: dict, task_to_id: dict,
                 no_timestamps: int | None = 50363) -> None:
        self.sot = 50258
        self.lang_to_id = lang_to_id
        self.task_to_id = task_to_id
        self.no_timestamps = no_timestamps

    _lookup_special = staticmethod(wo.WhisperOnnx._lookup_special)
    _initial_tokens = wo.WhisperOnnx._initial_tokens


class TestInitialTokens:

    ANGLE_LANG = {"<|en|>": 50259, "<|fr|>": 50265}
    BARE_TASK = {"transcribe": 50359, "translate": 50358}
    ANGLE_TASK = {"<|transcribe|>": 50359, "<|translate|>": 50358}

    def test_handles_the_mixed_key_spellings_shipped_by_real_models(self) -> None:
        """lang_to_id is keyed '<|en|>' and task_to_id is keyed 'transcribe'
        in the published configs. Assuming a single spelling drops the task
        token, and a short prompt does not raise -- Whisper transcribes a
        plausible phrase and then loops."""
        m = _FakeModel(self.ANGLE_LANG, self.BARE_TASK)
        assert m._initial_tokens("en", "transcribe") == [50258, 50259, 50359, 50363]

    def test_handles_angle_bracket_task_keys_too(self) -> None:
        m = _FakeModel(self.ANGLE_LANG, self.ANGLE_TASK)
        assert m._initial_tokens("en", "transcribe") == [50258, 50259, 50359, 50363]

    def test_prompt_order_is_sot_lang_task_notimestamps(self) -> None:
        m = _FakeModel(self.ANGLE_LANG, self.BARE_TASK)
        tokens = m._initial_tokens("fr", "translate")
        assert tokens == [50258, 50265, 50358, 50363]

    def test_unknown_task_raises_rather_than_dropping_the_token(self) -> None:
        m = _FakeModel(self.ANGLE_LANG, self.BARE_TASK)
        with pytest.raises(wo.WhisperOnnxError, match="task"):
            m._initial_tokens("en", "sing")

    def test_unknown_language_raises(self) -> None:
        m = _FakeModel(self.ANGLE_LANG, self.BARE_TASK)
        with pytest.raises(wo.WhisperOnnxError, match="language"):
            m._initial_tokens("kl", "transcribe")

    def test_english_only_models_get_a_bare_prompt(self) -> None:
        """whisper-*.en publish no language/task tables; the prompt is just
        sot + notimestamps, and demanding a task token would break them."""
        m = _FakeModel({}, {})
        assert m._initial_tokens("en", "transcribe") == [50258, 50363]

    def test_model_without_notimestamps_token(self) -> None:
        m = _FakeModel(self.ANGLE_LANG, self.BARE_TASK, no_timestamps=None)
        assert m._initial_tokens("en", "transcribe") == [50258, 50259, 50359]


# ---------------------------------------------------------------------------
# Repo resolution.
# ---------------------------------------------------------------------------

class TestResolveRepo:

    def test_bare_size(self) -> None:
        assert wo.resolve_repo("small") == "onnx-community/whisper-small"

    def test_maps_an_mlx_repo_id_to_its_onnx_equivalent(self) -> None:
        """--model stays portable across backends instead of meaning something
        different on each platform."""
        assert (wo.resolve_repo("mlx-community/whisper-large-v3-turbo")
                == "onnx-community/whisper-large-v3-turbo")

    def test_turbo_alias(self) -> None:
        assert wo.resolve_repo("turbo") == "onnx-community/whisper-large-v3-turbo"

    def test_explicit_repo_id_passes_through(self) -> None:
        assert wo.resolve_repo("someone/custom-whisper") == "someone/custom-whisper"

    def test_unmappable_bare_name_raises(self) -> None:
        with pytest.raises(wo.WhisperOnnxError, match="don't know how to map"):
            wo.resolve_repo("enormous")


# ---------------------------------------------------------------------------
# Audio decode boundary.
# ---------------------------------------------------------------------------

class TestDecodeAudio:

    def test_missing_ffmpeg_is_a_clear_error(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ffmpeg is an external prerequisite, so its absence should say so
        rather than surfacing as a FileNotFoundError from subprocess."""
        monkeypatch.setattr(wo.shutil, "which", lambda _: None)
        with pytest.raises(wo.WhisperOnnxError, match="ffmpeg"):
            wo.decode_audio(tmp_path / "nope.mp3")

    def test_ffmpeg_failure_surfaces_its_stderr(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
            allow_subprocess: None) -> None:
        from types import SimpleNamespace
        monkeypatch.setattr(wo.shutil, "which", lambda _: "ffmpeg")
        monkeypatch.setattr(
            wo.subprocess, "run",
            lambda *a, **k: SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"moov atom not found"))
        with pytest.raises(wo.WhisperOnnxError, match="moov atom"):
            wo.decode_audio(tmp_path / "broken.mp3")

    def test_pcm_is_scaled_to_unit_range(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
            allow_subprocess: None) -> None:
        from types import SimpleNamespace
        pcm = np.array([0, 16384, -16384, 32767], dtype=np.int16).tobytes()
        monkeypatch.setattr(wo.shutil, "which", lambda _: "ffmpeg")
        monkeypatch.setattr(
            wo.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=pcm, stderr=b""))
        audio = wo.decode_audio(tmp_path / "ok.wav")
        assert audio.dtype == np.float32
        assert audio.max() <= 1.0 and audio.min() >= -1.0
        assert audio[1] == pytest.approx(0.5, abs=1e-4)
