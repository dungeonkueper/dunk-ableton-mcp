"""Spectrogram images (PNG, no plotting library needed).

Two sources:
* :func:`from_bands` - a band-by-time matrix from the live feed (31 rows),
* :func:`from_audio` - an STFT of a file, mapped to a log-frequency axis.

Low frequencies are at the bottom. Faint grid lines mark 100 Hz, 1 kHz and
10 kHz. Colour: dark blue (quiet) -> red -> yellow -> white (loud), over a
``range_db`` window below the loudest cell.
"""

from __future__ import annotations

import struct
import zlib
from typing import Optional, Sequence, Tuple

import numpy as np
from scipy import signal

from . import thirds

_STOPS = np.array([
    [0.00, 0, 0, 20], [0.25, 40, 10, 110], [0.50, 190, 30, 60],
    [0.75, 250, 160, 20], [1.00, 255, 255, 230]], float)


def colormap(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    out = np.empty(v.shape + (3,), np.uint8)
    for c in range(3):
        out[..., c] = np.interp(v, _STOPS[:, 0], _STOPS[:, c + 1]).astype(np.uint8)
    return out


def png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an (h, w, 3) uint8 array as PNG."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\0" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _render(db: np.ndarray, freqs: np.ndarray, range_db: float, width: int, height: int) -> bytes:
    """db: (n_freq_rows, n_time) with rows ascending in frequency."""
    top = np.nanmax(db) if np.isfinite(db).any() else 0.0
    norm = (np.nan_to_num(db, nan=top - range_db) - (top - range_db)) / range_db
    # resample to the output grid (nearest in time, linear over log-frequency rows)
    t_idx = np.minimum((np.arange(width) * norm.shape[1] / width).astype(int), norm.shape[1] - 1)
    logf = np.log10(freqs)
    rows = np.linspace(logf[0], logf[-1], height)
    img = np.empty((height, width))
    for x, ti in enumerate(t_idx):
        img[:, x] = np.interp(rows, logf, norm[:, ti])
    rgb = colormap(img[::-1])
    for f in (100.0, 1000.0, 10000.0):
        if freqs[0] < f < freqs[-1]:
            y = height - 1 - int(round((np.log10(f) - logf[0]) / (logf[-1] - logf[0]) * (height - 1)))
            rgb[y, ::4] = (255, 255, 255)
    return png_bytes(rgb)


def from_bands(band_rms: np.ndarray, range_db: float = 60.0, width: int = 600, height: int = 248) -> bytes:
    """band_rms: (n_frames, 31) linear RMS values from the live feed."""
    m = np.asarray(band_rms, float)
    if m.ndim != 2 or m.shape[1] != thirds.N_BANDS or m.shape[0] < 1:
        raise ValueError("need at least one frame of 31 bands")
    return _render(thirds.to_db(m.T ** 2), thirds.CENTRES, range_db, width, height)


def from_audio(x: np.ndarray, sr: float, range_db: float = 80.0, width: int = 800, height: int = 300,
               f_range: Tuple[float, float] = (20.0, 20000.0)) -> bytes:
    """x: (n, channels) or (n,) samples; the mid signal is analysed."""
    x = np.asarray(x, float)
    mono = x.mean(axis=1) if x.ndim == 2 else x
    nfft = 4096 if sr <= 50000 else 8192
    hop = max(nfft // 4, int(mono.size / (width * 2)) or 1)
    f, _, z = signal.stft(mono, fs=sr, nperseg=nfft, noverlap=max(0, nfft - hop), boundary=None, padded=False)
    lo, hi = f_range[0], min(f_range[1], sr / 2.0)
    keep = (f >= lo) & (f <= hi)
    db = 10.0 * np.log10(np.abs(z[keep]) ** 2 + 1e-20)
    return _render(db, f[keep], range_db, width, height)
