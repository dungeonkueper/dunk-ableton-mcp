"""1/3-octave filter bank shared by the live analyzer feed and file analysis.

The Max for Live device (``m4l/MixEars.maxpat``) measures the mid signal
with ``fffb~``: 31 two-pole band-pass resonators, unity gain at the centre,
Q = 4.32 (a 1/3-octave bandwidth), centres ``1 kHz * 2^(k/3)`` from
~19.7 Hz to ~19.8 kHz. :func:`filterbank_sos` builds the same filters (RBJ
cookbook band-pass, 0 dB peak gain), so a reference file analysed here and
the live feed from Live produce comparable curves.

Conventions
-----------
* Band values are RMS amplitudes (linear) or their power in dB.
* A *relative curve* (:func:`relative_curve`) is each band's power divided by
  the sum of all bands, in dB. It is loudness-independent: two curves can be
  compared directly, and a constant level offset (e.g. the resonators' noise
  bandwidth being wider than an ideal brick-wall third) cancels out.
* ``tilt_curve`` builds a smooth target with a given slope in dB per octave of
  *band power*. Pink noise is flat (0 dB/oct) in band power.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np
from scipy import signal

N_BANDS = 31
Q_THIRD = 1.0 / (2.0 ** (1.0 / 6.0) - 2.0 ** (-1.0 / 6.0))  # 4.318
CENTRES = np.array([1000.0 * 2.0 ** ((k - 17) / 3.0) for k in range(N_BANDS)])
NOMINAL = ["20", "25", "31.5", "40", "50", "63", "80", "100", "125", "160", "200", "250", "315",
           "400", "500", "630", "800", "1k", "1.25k", "1.6k", "2k", "2.5k", "3.15k", "4k", "5k",
           "6.3k", "8k", "10k", "12.5k", "16k", "20k"]
EDGE = 2.0 ** (1.0 / 6.0)

EPS = 1e-20


def band_label(i: int) -> str:
    return NOMINAL[i] + " Hz"


def filterbank_sos(sr: float, q: float = Q_THIRD) -> List[Optional[np.ndarray]]:
    """One RBJ band-pass biquad (0 dB peak) per band; ``None`` above Nyquist."""
    out: List[Optional[np.ndarray]] = []
    for fc in CENTRES:
        if fc * EDGE >= sr / 2.0 * 0.98:
            out.append(None)
            continue
        w0 = 2.0 * math.pi * fc / sr
        alpha = math.sin(w0) / (2.0 * q)
        b = np.array([alpha, 0.0, -alpha])
        a = np.array([1.0 + alpha, -2.0 * math.cos(w0), 1.0 - alpha])
        out.append(np.concatenate([b / a[0], a / a[0]])[None, :])
    return out


class FilterBank:
    """Streaming filter bank: feed blocks, get per-band RMS for each block."""

    def __init__(self, sr: float):
        self.sr = float(sr)
        self.sos = filterbank_sos(sr)
        self.zi = [None if s is None else np.zeros((1, 2)) for s in self.sos]

    def process(self, mono: np.ndarray) -> np.ndarray:
        """Band RMS amplitudes for one block (zeros for bands above Nyquist)."""
        mono = np.asarray(mono, dtype=np.float64)
        rms = np.zeros(N_BANDS)
        for i, sos in enumerate(self.sos):
            if sos is None:
                continue
            y, self.zi[i] = signal.sosfilt(sos, mono, zi=self.zi[i])
            rms[i] = math.sqrt(float(np.mean(y * y))) if y.size else 0.0
        return rms


NOISE_GAIN = math.pi / 2.0  # resonator ENBW / ideal 1/3-octave width (analog prototype)
_CORR_CACHE: Dict[float, np.ndarray] = {}


def bank_correction(sr: float) -> np.ndarray:
    """Per-band factor that undoes the digital filters' frequency warping.

    The bilinear transform squeezes the resonators near Nyquist, so their
    noise bandwidth shrinks (about -2.5 dB in the 20 kHz band at 48 kHz).
    Multiplying raw band power by this factor gives every band the analog
    noise bandwidth ``NOISE_GAIN * fc / Q``. Zero for bands above Nyquist.
    """
    key = round(float(sr), 3)
    if key not in _CORR_CACHE:
        freqs = np.geomspace(1.0, sr / 2.0, 20000)
        out = np.zeros(N_BANDS)
        for i, sos in enumerate(filterbank_sos(sr)):
            if sos is None:
                continue
            _, h = signal.sosfreqz(sos, worN=freqs, fs=sr)
            enbw = np.trapezoid(np.abs(h) ** 2, freqs)
            out[i] = NOISE_GAIN * CENTRES[i] / Q_THIRD / enbw
        _CORR_CACHE[key] = out
    return _CORR_CACHE[key]


def band_powers(mono: np.ndarray, sr: float, skip_s: float = 0.1) -> np.ndarray:
    """Mean band power over a whole signal, warping-corrected (see bank_correction)."""
    mono = np.asarray(mono, dtype=np.float64)
    skip = min(int(skip_s * sr), mono.size // 4)
    out = np.zeros(N_BANDS)
    for i, sos in enumerate(filterbank_sos(sr)):
        if sos is None:
            continue
        y = signal.sosfilt(sos, mono)[skip:]
        out[i] = float(np.mean(y * y)) if y.size else 0.0
    return out * bank_correction(sr)


def to_db(power) -> np.ndarray:
    return 10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64), EPS))


def relative_curve(power: Sequence[float], valid: Optional[Sequence[bool]] = None) -> np.ndarray:
    """Band power relative to the total of the valid bands, in dB (NaN = invalid)."""
    p = np.asarray(power, dtype=np.float64)
    mask = np.ones(N_BANDS, bool) if valid is None else np.asarray(valid, bool)
    mask &= p > 0
    total = float(np.sum(p[mask]))
    out = np.full(N_BANDS, np.nan)
    if total <= EPS:
        return out
    out[mask] = to_db(p[mask] / total)
    return out


def tilt_curve(db_per_oct: float = -3.0, pivot_hz: float = 1000.0) -> np.ndarray:
    """Relative target curve with a constant slope in band power per octave."""
    raw = db_per_oct * np.log2(CENTRES / pivot_hz)
    return relative_curve(10.0 ** (raw / 10.0))


def k_weight_gain(freqs: np.ndarray) -> np.ndarray:
    """|H|^2 of the BS.1770 K-weighting filter at the given frequencies (48 kHz design)."""
    from .loudness import k_weighting_sos

    _, h = signal.sosfreqz(k_weighting_sos(48000.0), worN=np.asarray(freqs, float), fs=48000.0)
    return np.abs(h) ** 2


K_GAIN = None


def k_weighted_power(power: Sequence[float]) -> float:
    """K-weighted mean-square estimated from (warping-corrected) band powers."""
    global K_GAIN
    if K_GAIN is None:
        K_GAIN = k_weight_gain(CENTRES)
    return float(np.sum(np.asarray(power, dtype=np.float64) * K_GAIN) / NOISE_GAIN)


def curve_dict(curve_db: Sequence[float], nd: int = 1) -> Dict[str, Optional[float]]:
    """{"20 Hz": -31.2, ...} with None for invalid bands."""
    out = {}
    for i, v in enumerate(curve_db):
        out[NOMINAL[i]] = None if v is None or not math.isfinite(float(v)) else round(float(v), nd)
    return out
