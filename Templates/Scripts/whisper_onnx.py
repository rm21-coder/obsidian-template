#!/usr/bin/env python3
"""
whisper_onnx.py — Whisper transcription on ONNX Runtime, with no torch.

Why this exists: `faster-whisper` is unusable on Windows ARM64 because its
backend, ctranslate2, publishes no win_arm64 wheel and no sdist, and torch has
no win_arm64 wheel either, which rules out openai-whisper as well. ONNX Runtime
*does* ship a native ARM64 build, so this module drives a pre-exported Whisper
ONNX model directly: feature extraction in numpy, greedy decoding against the
merged decoder, tokenisation via `tokenizers`. Nothing here is Windows- or
ARM-specific -- it is simply a torch-free backend, and runs anywhere ONNX
Runtime does.

Model source: the `onnx-community/whisper-*` exports (the transformers.js
layout) -- an `onnx/encoder_model.onnx` plus an `onnx/decoder_model_merged.onnx`
carrying both the no-cache and with-cache branches, selected by the
`use_cache_branch` input.

Layer counts, head counts, mel-bin counts and the special-token ids are all
read from the model's own config.json / generation_config.json rather than
hardcoded, so tiny through large-v3-turbo work unchanged (large-v3 uses 128
mel bins where earlier models use 80, and turbo has fewer decoder layers).

Audio is decoded by ffmpeg, which must be on PATH.

Limitation: decoding runs in `<|notimestamps|>` mode, so timestamps are
per-30s-window rather than per-utterance. Segment boundaries land on window
boundaries. That is accurate enough to navigate a transcript and keeps the
decode loop simple; word-level timing would need the timestamp-token path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
CHUNK_SECONDS = 30
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_SECONDS      # 480000
N_FRAMES = CHUNK_SAMPLES // HOP_LENGTH           # 3000

# Whisper's own cap on decoder positions; also our per-window token budget.
MAX_DECODE_TOKENS = 448


class WhisperOnnxError(RuntimeError):
    """Raised for unrecoverable setup problems (missing ffmpeg, bad model)."""


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def decode_audio(path: Path, *, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any ffmpeg-readable file to mono float32 PCM in [-1, 1]."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise WhisperOnnxError(
            "ffmpeg not found on PATH. It decodes the audio before "
            "transcription. Install it (winget install Gyan.FFmpeg) and make "
            "sure its bin directory is on PATH.")
    proc = subprocess.run(
        [exe, "-nostdin", "-threads", "0", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le",
         "-ar", str(sample_rate), "-"],
        capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "ignore").strip().splitlines()[-3:]
        raise WhisperOnnxError(f"ffmpeg failed on {path}: {' / '.join(tail)}")
    return np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Log-mel features
#
# Reimplements openai/whisper's log_mel_spectrogram in numpy. The mel
# filterbank matches librosa.filters.mel(sr, n_fft, n_mels) with its defaults
# (the Slaney mel scale and Slaney normalisation) -- which is what Whisper's
# shipped mel_filters.npz was generated with. Getting either the scale or the
# normalisation wrong yields a spectrogram the encoder was never trained on,
# and the model degrades into plausible-sounding nonsense rather than failing
# outright, so this is worth pinning with tests.
# ---------------------------------------------------------------------------

def _hz_to_mel(freq: np.ndarray | float) -> np.ndarray:
    freq = np.asanyarray(freq, dtype=float)
    f_min, f_sp = 0.0, 200.0 / 3
    mels = (freq - f_min) / f_sp
    min_log_hz, min_log_mel = 1000.0, (1000.0 - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = freq >= min_log_hz
    mels = np.where(log_t,
                    min_log_mel + np.log(np.maximum(freq, 1e-10) / min_log_hz) / logstep,
                    mels)
    return mels


def _mel_to_hz(mels: np.ndarray) -> np.ndarray:
    mels = np.asanyarray(mels, dtype=float)
    f_min, f_sp = 0.0, 200.0 / 3
    freqs = f_min + f_sp * mels
    min_log_hz, min_log_mel = 1000.0, (1000.0 - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = mels >= min_log_mel
    return np.where(log_t,
                    min_log_hz * np.exp(logstep * (mels - min_log_mel)),
                    freqs)


def mel_filterbank(sample_rate: int, n_fft: int, n_mels: int) -> np.ndarray:
    """[n_mels, n_fft//2 + 1] Slaney-normalised triangular mel filters."""
    fftfreqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    mel_pts = np.linspace(_hz_to_mel(0.0), _hz_to_mel(sample_rate / 2.0), n_mels + 2)
    hz_pts = _mel_to_hz(mel_pts)

    fdiff = np.diff(hz_pts)
    ramps = hz_pts[:, None] - fftfreqs[None, :]
    weights = np.zeros((n_mels, len(fftfreqs)), dtype=np.float32)
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0.0, np.minimum(lower, upper))

    # Slaney normalisation: equal area per filter rather than equal peak.
    enorm = 2.0 / (hz_pts[2:n_mels + 2] - hz_pts[:n_mels])
    weights *= enorm[:, None]
    return weights


def log_mel_spectrogram(audio: np.ndarray, n_mels: int) -> np.ndarray:
    """[n_mels, frames] log-mel features, matching Whisper's preprocessing."""
    # torch.stft(center=True) reflect-pads by n_fft // 2 before framing.
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode="reflect")
    # Periodic Hann, matching torch.hann_window's default.
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT)

    n_frames = 1 + (len(padded) - N_FFT) // HOP_LENGTH
    frames = np.lib.stride_tricks.as_strided(
        padded,
        shape=(n_frames, N_FFT),
        strides=(padded.strides[0] * HOP_LENGTH, padded.strides[0]),
    )
    spec = np.fft.rfft(frames * window, axis=-1)
    # Whisper drops the final frame, then squares the magnitude.
    magnitudes = (np.abs(spec[:-1]) ** 2).T.astype(np.float32)

    mel_spec = mel_filterbank(SAMPLE_RATE, N_FFT, n_mels) @ magnitudes
    log_spec = np.log10(np.maximum(mel_spec, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)


def pad_or_trim_audio(audio: np.ndarray,
                      length: int = CHUNK_SAMPLES) -> np.ndarray:
    """Pad/trim a waveform to exactly one 30s window.

    Padding happens here, in the time domain, and NOT on the feature matrix.
    Zero-padding the features instead looks equivalent and is not: log-mel of
    digital silence is not 0.0 but roughly (max - 8 + 4) / 4, so zeros in
    feature space form a block unlike anything the encoder saw in training.
    The failure mode is quiet -- the model transcribes the real audio, then
    terminates early at the boundary rather than erroring.
    """
    if len(audio) > length:
        return audio[:length]
    if len(audio) < length:
        return np.pad(audio, (0, length - len(audio)), constant_values=0.0)
    return audio


def pad_or_trim_features(features: np.ndarray) -> np.ndarray:
    """Force a feature block to exactly N_FRAMES columns.

    A backstop for off-by-one framing only. Windows should already be padded
    in the time domain by pad_or_trim_audio -- see the note there.
    """
    if features.shape[-1] > N_FRAMES:
        return features[:, :N_FRAMES]
    if features.shape[-1] < N_FRAMES:
        pad = N_FRAMES - features.shape[-1]
        return np.pad(features, ((0, 0), (0, pad)), mode="edge")
    return features


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class WhisperOnnx:
    """A Whisper ONNX export, loaded once and reusable across windows."""

    def __init__(self, model_dir: Path, *, providers: list[str] | None = None,
                 intra_op_threads: int | None = None,
                 variant: str = "") -> None:
        import onnxruntime as ort

        self.model_dir = Path(model_dir)
        self.variant = variant
        enc = self._locate("encoder_model")
        dec = self._locate("decoder_model_merged")

        opts = ort.SessionOptions()
        if intra_op_threads:
            opts.intra_op_num_threads = intra_op_threads
        providers = providers or ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(str(enc), opts, providers=providers)
        self.decoder = ort.InferenceSession(str(dec), opts, providers=providers)

        cfg = json.loads((self.model_dir / "config.json").read_text(encoding="utf-8"))
        gen = json.loads(
            (self.model_dir / "generation_config.json").read_text(encoding="utf-8"))

        self.n_mels: int = cfg.get("num_mel_bins", 80)
        self.max_positions: int = cfg.get("max_target_positions", MAX_DECODE_TOKENS)
        self.layers: int = cfg.get("decoder_layers", 6)
        self.heads: int = cfg.get("decoder_attention_heads", 8)
        self.head_dim: int = cfg["d_model"] // self.heads

        self.sot: int = gen.get("decoder_start_token_id", 50258)
        self.eos: int = gen.get("eos_token_id", 50257)
        self.no_timestamps: int | None = gen.get("no_timestamps_token_id")
        self.lang_to_id: dict = gen.get("lang_to_id", {}) or {}
        self.task_to_id: dict = gen.get("task_to_id", {}) or {}
        # Tokens Whisper never emits as text (special markers, non-speech).
        self.suppress: list[int] = list(gen.get("suppress_tokens", []) or [])
        self.begin_suppress: list[int] = list(gen.get("begin_suppress_tokens", []) or [])

        from tokenizers import Tokenizer
        self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))

    def _locate(self, stem: str) -> Path:
        """Find a model file, honouring an explicitly requested variant.

        The onnx-community exports ship quantised variants beside the full
        weights. Those are materially faster on CPU, but quantisation costs
        transcription accuracy in ways that are hard to notice by eye -- the
        output stays fluent and simply drifts from what was said. So the
        default is the full-precision export, and a caller has to ask for a
        quantised one by name rather than getting it silently.
        """
        onnx_dir = self.model_dir / "onnx"
        if self.variant:
            candidate = onnx_dir / f"{stem}_{self.variant}.onnx"
            if candidate.exists():
                return candidate
            raise WhisperOnnxError(
                f"variant '{self.variant}' not published for '{stem}' in "
                f"{onnx_dir}. Available: "
                f"{', '.join(sorted(p.name for p in onnx_dir.glob(f'{stem}*.onnx')))}")
        candidate = onnx_dir / f"{stem}.onnx"
        if candidate.exists():
            return candidate
        raise WhisperOnnxError(
            f"no ONNX file for '{stem}' under {onnx_dir}. Expected the "
            f"onnx-community layout (onnx/{stem}.onnx).")

    # -- decoding ----------------------------------------------------------

    @staticmethod
    def _lookup_special(table: dict, name: str) -> int | None:
        """Resolve a special token by name, tolerating both key spellings.

        These two tables disagree with each other in the published configs:
        lang_to_id is keyed '<|en|>' while task_to_id is keyed 'transcribe',
        bare. Assuming one spelling silently drops the other token, and a
        short prefix does not raise -- Whisper decodes a plausible phrase and
        then degenerates into repetition, which reads like a model quality
        problem rather than a prompt bug.
        """
        for key in (f"<|{name}|>", name):
            if key in table:
                return table[key]
        return None

    def _initial_tokens(self, language: str, task: str) -> list[int]:
        """Build Whisper's decoder prompt: sot, language, task, notimestamps.

        Order matters and so does completeness -- the model was trained with
        every one of these present (English-only checkpoints excepted, which
        publish no lang/task tables at all).
        """
        tokens = [self.sot]

        if self.lang_to_id:
            lang_id = self._lookup_special(self.lang_to_id, language)
            if lang_id is None:
                raise WhisperOnnxError(
                    f"language '{language}' is not one of the "
                    f"{len(self.lang_to_id)} this model supports")
            tokens.append(lang_id)

        if self.task_to_id:
            task_id = self._lookup_special(self.task_to_id, task)
            if task_id is None:
                raise WhisperOnnxError(
                    f"task '{task}' not in this model's task table "
                    f"({sorted(self.task_to_id)})")
            tokens.append(task_id)

        if self.no_timestamps is not None:
            tokens.append(self.no_timestamps)
        return tokens

    def _empty_past(self) -> dict[str, np.ndarray]:
        empty = np.zeros((1, self.heads, 0, self.head_dim), dtype=np.float32)
        past: dict[str, np.ndarray] = {}
        for i in range(self.layers):
            for kind in ("decoder", "encoder"):
                for part in ("key", "value"):
                    past[f"past_key_values.{i}.{kind}.{part}"] = empty
        return past

    def decode_window(self, features: np.ndarray, *, language: str,
                      task: str) -> list[int]:
        """Greedy-decode one 30s feature window to token ids."""
        encoder_out = self.encoder.run(
            None, {"input_features": features[None, :, :]})[0]

        tokens = self._initial_tokens(language, task)
        # The decoder has a hard positional limit (max_target_positions, 448).
        # The prompt tokens occupy positions too, so the generation budget is
        # what's left after them -- overrunning it doesn't degrade gracefully,
        # it fails inside the model's positional-embedding gather.
        budget = max(1, self.max_positions - len(tokens) - 1)
        past = self._empty_past()
        # Cross-attention KV is a function of the encoder output alone, so it
        # is computed once on the first pass and reused for every step after.
        encoder_kv: dict[str, np.ndarray] = {}
        generated: list[int] = []
        first = True

        for step in range(budget):
            input_ids = (np.array([tokens], dtype=np.int64) if first
                         else np.array([[generated[-1]]], dtype=np.int64))
            feeds = {
                "input_ids": input_ids,
                "encoder_hidden_states": encoder_out,
                "use_cache_branch": np.array([not first], dtype=bool),
            }
            feeds.update(past)
            if not first:
                feeds.update(encoder_kv)

            outputs = self.decoder.run(None, feeds)
            names = [o.name for o in self.decoder.get_outputs()]
            result = dict(zip(names, outputs))
            logits = result["logits"][0, -1, :].astype(np.float32)

            banned = list(self.suppress)
            if step == 0:
                banned += self.begin_suppress
            # Timestamp tokens sit above the vocabulary proper; in
            # notimestamps mode emitting one is always a decode error.
            if self.no_timestamps is not None:
                logits[self.no_timestamps + 1:] = -np.inf
            for t in banned:
                if 0 <= t < logits.shape[0]:
                    logits[t] = -np.inf

            next_token = int(np.argmax(logits))
            if next_token == self.eos:
                break
            generated.append(next_token)

            for i in range(self.layers):
                for part in ("key", "value"):
                    past[f"past_key_values.{i}.decoder.{part}"] = \
                        result[f"present.{i}.decoder.{part}"]
            if first:
                for i in range(self.layers):
                    for part in ("key", "value"):
                        encoder_kv[f"past_key_values.{i}.encoder.{part}"] = \
                            result[f"present.{i}.encoder.{part}"]
                first = False

        return generated

    def transcribe(self, audio: np.ndarray, *, language: str = "en",
                   task: str = "transcribe",
                   progress: callable | None = None) -> dict:
        """Transcribe a full waveform, one 30s window at a time.

        Returns the same shape the MLX and faster-whisper backends return, so
        podcast_transcribe.py's writer needs no special-casing.
        """
        segments: list[dict] = []
        parts: list[str] = []
        total = max(1, int(np.ceil(len(audio) / CHUNK_SAMPLES)))

        for index, start in enumerate(range(0, max(len(audio), 1), CHUNK_SAMPLES)):
            window = audio[start:start + CHUNK_SAMPLES]
            if len(window) == 0:
                break
            actual_end = min(start + len(window), len(audio))
            features = pad_or_trim_features(
                log_mel_spectrogram(pad_or_trim_audio(window), self.n_mels))
            token_ids = self.decode_window(features, language=language, task=task)
            text = self.tokenizer.decode(token_ids, skip_special_tokens=True).strip()
            if text:
                segments.append({
                    "start": start / SAMPLE_RATE,
                    "end": actual_end / SAMPLE_RATE,
                    "text": text,
                })
                parts.append(text)
            if progress:
                progress(index + 1, total)

        return {
            "segments": segments,
            "text": " ".join(parts).strip(),
            "language": language,
            "_audio_duration": len(audio) / SAMPLE_RATE,
        }


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

# The MLX backend takes repo ids like 'mlx-community/whisper-large-v3-turbo'.
# Map the size out of whatever the caller passed so --model stays portable
# across backends instead of meaning something different on each platform.
_SIZE_ALIASES = {
    "tiny": "onnx-community/whisper-tiny",
    "base": "onnx-community/whisper-base",
    "small": "onnx-community/whisper-small",
    "medium": "onnx-community/whisper-medium",
    "large-v3-turbo": "onnx-community/whisper-large-v3-turbo",
    "large-v3": "onnx-community/whisper-large-v3",
    "turbo": "onnx-community/whisper-large-v3-turbo",
}


def resolve_repo(model: str) -> str:
    """Map a bare size, an MLX repo id, or an explicit repo id to an ONNX repo."""
    if model in _SIZE_ALIASES:
        return _SIZE_ALIASES[model]
    tail = model.rsplit("/", 1)[-1]
    if tail.startswith("whisper-"):
        size = tail[len("whisper-"):]
        if size in _SIZE_ALIASES:
            return _SIZE_ALIASES[size]
    if "/" in model:
        return model          # an explicit repo id we should not second-guess
    raise WhisperOnnxError(
        f"don't know how to map model '{model}' to an ONNX repo. Pass a size "
        f"({', '.join(sorted(_SIZE_ALIASES))}) or a full HuggingFace repo id.")


def ensure_model(model: str, *, variant: str = "",
                 cache_dir: Path | None = None) -> Path:
    """Download (once) and return the local path to an ONNX Whisper export.

    Only the two files actually loaded are fetched. The onnx-community repos
    publish every quantisation side by side, so a wildcard pulls fp16, q4,
    int8 and more along for the ride -- for whisper-small that is 2.8 GB
    instead of the ~1 GB in use.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise WhisperOnnxError(
            "huggingface-hub is required to fetch the ONNX model") from exc

    suffix = f"_{variant}" if variant else ""
    repo = resolve_repo(model)
    path = snapshot_download(
        repo,
        allow_patterns=[
            f"onnx/encoder_model{suffix}.onnx",
            f"onnx/encoder_model{suffix}.onnx_data",      # externalised weights
            f"onnx/decoder_model_merged{suffix}.onnx",
            f"onnx/decoder_model_merged{suffix}.onnx_data",
            "*.json",
        ],
        cache_dir=str(cache_dir) if cache_dir else None,
    )
    return Path(path)
