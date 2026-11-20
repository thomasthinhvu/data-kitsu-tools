#!/usr/bin/env python3
"""
Retro 8-bit chiptune sound effects synthesizer & cross-platform player.

Generates crisp 8-bit PCM WAV files on demand using only the Python stdlib
(wave, struct, math, random) and plays them asynchronously via QtMultimedia
(Windows, macOS, Linux) with a winsound fallback on Windows.
"""
from __future__ import annotations

import math
import os
import random
import struct
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 22050


def _square(phase: float) -> float:
    return 1.0 if math.sin(2.0 * math.pi * phase) >= 0.0 else -1.0


def _pulse(phase: float, duty: float = 0.25) -> float:
    return 1.0 if (phase % 1.0) < duty else -1.0


def _render_notes(notes: list[tuple[float, float]], volume: float = 0.14, duty: float = 0.25) -> bytes:
    """Render a sequence of ``(freq_hz, duration_s)`` into 16-bit mono PCM."""
    buf = bytearray()
    for freq, dur in notes:
        n_samples = max(1, int(SAMPLE_RATE * dur))
        for i in range(n_samples):
            t = i / SAMPLE_RATE
            env = max(0.0, 1.0 - (i / n_samples) * 0.6)
            if freq > 0:
                lead = _pulse(freq * t, duty)
                bass = _square((freq * 0.5) * t) * 0.35
                val = (lead * 0.75 + bass) * volume * env
            else:
                val = 0.0
            sample = max(-32767, min(32767, int(val * 32767)))
            buf.extend(struct.pack("<h", sample))
    return bytes(buf)


def _render_trash_clear(volume: float = 0.14) -> bytes:
    """Render an 8-bit trash-dump sound (descending notes + noise crumple + thud)."""
    rng = random.Random(42)
    buf = bytearray()
    # Part 1: quick playful 3-note drop before the bin whoosh
    buf.extend(_render_notes([(659.25, 0.045), (523.25, 0.045), (392.00, 0.05)], volume=volume * 0.9, duty=0.25))

    # Part 2: descending 8-bit whoosh (130ms)
    n1 = int(SAMPLE_RATE * 0.13)
    phase = 0.0
    for i in range(n1):
        frac = i / n1
        freq = 540.0 * (1.0 - 0.72 * frac)
        phase += freq / SAMPLE_RATE
        sq = 1.0 if (phase % 1.0) < 0.5 else -1.0
        noise = (rng.randint(-1, 1)) * 0.45
        env = 1.0 - 0.4 * frac
        val = (sq * 0.6 + noise * 0.4) * volume * env
        buf.extend(struct.pack("<h", max(-32767, min(32767, int(val * 32767)))))

    # Part 3: 8-bit paper/bin crunch + low double-thud (160ms)
    n2 = int(SAMPLE_RATE * 0.16)
    hold = 0.0
    for i in range(n2):
        frac = i / n2
        if i % 6 == 0:  # quantized low-sample-rate 8-bit noise
            hold = rng.choice([-1.0, -0.5, 0.5, 1.0])
        thud = _square(95.0 * (1.0 - 0.45 * frac) * (i / SAMPLE_RATE))
        env = (1.0 - frac) ** 1.4
        val = (hold * 0.55 + thud * 0.45) * volume * env
        buf.extend(struct.pack("<h", max(-32767, min(32767, int(val * 32767)))))
    return bytes(buf)


def _write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)


class RetroSfx:
    """Lazy-initialized 8-bit sound effect player."""

    SOUNDS = ("scan", "start", "stop", "done", "clear")

    def __init__(self, parent=None) -> None:
        self._parent = parent
        self._effects: dict[str, object] = {}
        self._paths: dict[str, Path] = {}
        self._initialized = False
        self.enabled = True

    def _ensure_files(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        base = Path(tempfile.gettempdir()) / "data_kitsu_tools_sfx"
        recipes = {
            "scan": lambda: _render_notes([
                (440.00, 0.045), (523.25, 0.045), (659.25, 0.045),
                (587.33, 0.045), (783.99, 0.06), (1046.50, 0.10),
            ]),
            "start": lambda: _render_notes([
                (392.00, 0.05), (523.25, 0.05), (659.25, 0.05),
                (783.99, 0.06), (659.25, 0.045), (1046.50, 0.13),
            ]),
            "stop": lambda: _render_notes([
                (783.99, 0.055), (659.25, 0.055), (587.33, 0.055),
                (523.25, 0.06), (392.00, 0.13),
            ]),
            "done": lambda: _render_notes([
                (523.25, 0.06), (659.25, 0.06), (783.99, 0.06),
                (1046.50, 0.09), (783.99, 0.06), (1046.50, 0.22),
            ]),
            "clear": lambda: _render_trash_clear(),
        }
        for name, builder in recipes.items():
            wav_path = base / f"{name}_v2.wav"
            try:
                if not wav_path.is_file() or wav_path.stat().st_size == 0:
                    _write_wav(wav_path, builder())
                self._paths[name] = wav_path
            except OSError:
                pass

    def play(self, name: str) -> None:
        if not self.enabled or name not in self.SOUNDS:
            return
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        self._ensure_files()
        wav_path = self._paths.get(name)
        if wav_path is None or not wav_path.is_file():
            return

        if os.name == "nt":
            try:
                import winsound

                winsound.PlaySound(
                    str(wav_path),
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return
            except Exception:
                pass

        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

            eff = self._effects.get(name)
            if eff is None:
                eff = QSoundEffect(self._parent)
                eff.setSource(QUrl.fromLocalFile(str(wav_path)))
                eff.setVolume(0.22)
                self._effects[name] = eff
            eff.play()
        except Exception:
            pass
