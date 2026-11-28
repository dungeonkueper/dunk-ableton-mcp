"""Mix diagnosis on 1/3-octave curves: reference deviation, EQ moves, masking.

Everything here is pure (numpy only) and works on the 31-band curves from
``thirds``. The numbers are heuristics meant to give an assistant a concrete,
explainable starting point; the final judgement stays with the listener.

EQ moves are *conservative*: they correct about 60 % of a smoothed deviation,
never more than 6 dB per move, at most ``max_moves`` moves.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from . import thirds

TOLERANCE_DB = 1.5
CORRECTION = 0.6
MAX_GAIN_DB = 6.0
# Ears are less sensitive at the extremes; widen the tolerance there.
_TOL_SCALE = np.where(thirds.CENTRES < 40, 2.0, np.where(thirds.CENTRES > 12500, 1.6, 1.0))

PROBLEM_ZONES = [
    (20, 60, "sub", "rumble / sub weight"),
    (60, 160, "bass", "boom / punch"),
    (160, 500, "low_mid", "mud / body"),
    (500, 1600, "mid", "boxiness / honk"),
    (1600, 5000, "presence", "harshness / clarity"),
    (5000, 10000, "brilliance", "sizzle / sibilance"),
    (10000, 20000, "air", "air / hiss"),
]


def zone(freq: float) -> Dict[str, str]:
    for lo, hi, name, words in PROBLEM_ZONES:
        if lo <= freq < hi:
            return {"zone": name, "character": words}
    return {"zone": "air", "character": "air / hiss"}


def smooth(curve: np.ndarray) -> np.ndarray:
    """3-band weighted moving average that respects NaN (invalid) bands."""
    c = np.asarray(curve, float)
    out = np.full_like(c, np.nan)
    for i in range(c.size):
        w, s = 0.0, 0.0
        for j, wj in ((i - 1, 0.25), (i, 0.5), (i + 1, 0.25)):
            if 0 <= j < c.size and math.isfinite(c[j]):
                w += wj; s += wj * c[j]
        if w and math.isfinite(c[i]):
            out[i] = s / w
    return out


def deviation(mix_rel: Sequence[float], target_rel: Sequence[float]) -> np.ndarray:
    """mix - target per band (dB), re-centred so the bands' mean deviation is 0.

    Re-centring removes the arbitrary level offset between two relative
    curves whose valid bands differ (e.g. a 44.1 kHz file without the 20 kHz band).
    """
    d = np.asarray(mix_rel, float) - np.asarray(target_rel, float)
    ok = np.isfinite(d)
    if ok.any():
        d[ok] -= np.median(d[ok])
    return d


def _q_for_octaves(n_oct: float) -> float:
    r = 2.0 ** n_oct
    return math.sqrt(r) / (r - 1.0)


def suggest_eq(dev: Sequence[float], max_moves: int = 4, tolerance_db: float = TOLERANCE_DB) -> List[Dict[str, Any]]:
    """Turn a deviation curve (mix minus target) into a few EQ moves.

    Returns moves sorted by frequency, each with type (bell, low_shelf,
    high_shelf, low_cut), freq_hz, gain_db, q, the bands it covers and a reason.
    """
    d = smooth(np.asarray(dev, float))
    tol = tolerance_db * _TOL_SCALE
    sign = np.where(np.isfinite(d) & (np.abs(np.nan_to_num(d)) > tol), np.sign(np.nan_to_num(d)), 0)
    runs, i = [], 0
    while i < d.size:
        if sign[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < d.size and sign[j + 1] == sign[i]:
            j += 1
        runs.append((i, j))
        i = j + 1
    moves = []
    last = max(k for k in range(d.size) if math.isfinite(d[k])) if np.isfinite(d).any() else -1
    for i, j in runs:
        seg = d[i:j + 1]
        k = i + int(np.argmax(np.abs(seg)))
        peak = float(d[k])
        weights = np.abs(seg)
        fc = float(np.exp(np.sum(weights * np.log(thirds.CENTRES[i:j + 1])) / np.sum(weights)))
        gain = float(np.clip(-peak * CORRECTION, -MAX_GAIN_DB, MAX_GAIN_DB))
        # Width at half the peak deviation approximates a bell's bandwidth.
        half = int(np.sum(np.abs(seg) >= abs(peak) / 2.0))
        width_oct = max(half / 3.0, 2.0 / 3.0)
        kind, q = "bell", float(np.clip(_q_for_octaves(width_oct), 0.5, 4.0))
        if i <= 1 and peak > 0 and thirds.CENTRES[j] <= 50:
            kind, fc, gain, q = "low_cut", float(thirds.CENTRES[j] * thirds.EDGE), 0.0, 0.71
        elif i == 0 and j >= 1:
            kind, fc, q = "low_shelf", float(thirds.CENTRES[j]), 0.71
        elif j >= last and i < last and thirds.CENTRES[i] >= 5000:
            kind, fc, q = "high_shelf", float(thirds.CENTRES[i]), 0.71
        z = zone(thirds.CENTRES[k])
        too = "too much" if peak > 0 else "too little"
        moves.append({
            "type": kind, "freq_hz": round(fc, 0), "gain_db": round(gain, 1), "q": round(q, 2),
            "bands": [thirds.NOMINAL[i] + " Hz", thirds.NOMINAL[j] + " Hz"],
            "deviation_db": round(peak, 1),
            "reason": "%s %s around %s Hz (%s, %+.1f dB vs target)" % (too, z["character"], thirds.NOMINAL[k], z["zone"], peak),
            "_score": abs(peak) * math.sqrt(j - i + 1),
        })
    moves.sort(key=lambda m: -m["_score"])
    moves = sorted(moves[:max_moves], key=lambda m: m["freq_hz"])
    for m in moves:
        m.pop("_score")
    return moves


def masking(sources: Dict[str, np.ndarray], floor_db: float = -60.0, window_db: float = 6.0,
            top: int = 8) -> List[Dict[str, Any]]:
    """Find band ranges where two sources compete at similar level.

    ``sources`` maps a name to absolute band power (31 values, linear). A band
    counts as contested when both sources are above ``floor_db``, within 12 dB
    of the loudest source in that band, and within ``window_db`` of each other.
    """
    names = sorted(sources)
    db = {n: thirds.to_db(sources[n]) for n in names}
    loudest = np.max(np.array([db[n] for n in names]), axis=0) if names else None
    out = []
    for a_i, a in enumerate(names):
        for b in names[a_i + 1:]:
            da, dbb = db[a], db[b]
            ok = (da > floor_db) & (dbb > floor_db) & (np.minimum(da, dbb) > loudest - 12.0) \
                & (np.abs(da - dbb) < window_db)
            if not ok.any():
                continue
            score = np.where(ok, 1.0 - np.abs(da - dbb) / window_db, 0.0)
            # group contiguous contested bands
            i = 0
            while i < ok.size:
                if not ok[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < ok.size and ok[j + 1]:
                    j += 1
                k = i + int(np.argmax(score[i:j + 1]))
                quieter = a if da[k] < dbb[k] else b
                louder = b if quieter == a else a
                z = zone(thirds.CENTRES[k])
                out.append({
                    "pair": [a, b], "bands": [thirds.NOMINAL[i] + " Hz", thirds.NOMINAL[j] + " Hz"],
                    "focus_hz": round(float(thirds.CENTRES[k]), 0), "zone": z["zone"],
                    "level_db": {a: round(float(da[k]), 1), b: round(float(dbb[k]), 1)},
                    "severity": round(float(np.sum(score[i:j + 1])), 2),
                    "hint": ("carve %s by 2-4 dB around %s Hz (or sidechain it to %s); keep %s's fundamental there"
                             % (quieter, thirds.NOMINAL[k], louder, louder)),
                })
                i = j + 1
    out.sort(key=lambda m: -m["severity"])
    return out[:top]


# ------------------------------------------------------------- EQ Eight

FILTER_TYPE_PATTERNS = {
    "bell": [r"bell", r"peak"],
    "low_shelf": [r"low\s*shelf"],
    "high_shelf": [r"high\s*shelf"],
    "low_cut": [r"low\s*cut.*12", r"high\s*pass.*12", r"low\s*cut", r"high\s*pass"],
}
DEFAULT_EQ_BANDS = {"low_cut": 1, "low_shelf": 1, "high_shelf": 8}


def filter_type_item(items: Sequence[str], kind: str) -> str:
    for pattern in FILTER_TYPE_PATTERNS[kind]:
        hits = [it for it in items if re.search(pattern, it, re.IGNORECASE)]
        if hits:
            return hits[0]
    raise ValueError("EQ Eight has no filter type for %s (items: %s)" % (kind, ", ".join(items)))


def assign_bands(moves: List[Dict[str, Any]]) -> List[int]:
    """EQ Eight band numbers: cuts/shelves at the edges, bells 2..7 by frequency."""
    used, out = set(), []
    for m in moves:
        n = DEFAULT_EQ_BANDS.get(m["type"])
        if n is None or n in used:
            n = next(b for b in range(2, 8) if b not in used) if len(used) < 8 else None
        if n is None:
            raise ValueError("more than 8 EQ moves")
        used.add(n)
        out.append(n)
    return out


def eq8_changes(moves: List[Dict[str, Any]], type_items: Sequence[str], bands: Optional[List[int]] = None,
                channel: str = "A") -> Dict[str, Any]:
    """Map moves to an EQ Eight changes dict for set_device_parameters_by_name."""
    bands = bands or assign_bands(moves)
    changes: Dict[str, Any] = {}
    for n, m in zip(bands, moves):
        p = "%d %%s %s" % (n, channel)
        changes[p % "Filter On"] = True
        changes[p % "Filter Type"] = filter_type_item(type_items, m["type"])
        freq = float(m["freq_hz"])
        changes[p % "Frequency"] = ("%.2f kHz" % (freq / 1000.0)) if freq >= 1000 else ("%.0f Hz" % freq)
        if m["type"] != "low_cut":
            changes[p % "Gain"] = "%.1f dB" % m["gain_db"]
        changes[p % "Resonance"] = "%.2f" % m["q"]
        m["eq_band"] = n
    return changes


# ------------------------------------------------------------- profiles

def profile_dir() -> Path:
    if os.environ.get("ABLETON_MCP_PROFILE_DIR"):
        return Path(os.environ["ABLETON_MCP_PROFILE_DIR"])
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "ableton-mcp" / "mix-profiles"


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-.").lower()
    if not slug:
        raise ValueError("profile name must contain letters or digits")
    return slug[:64]


def save_profile(name: str, curve_rel: Sequence[float], meta: Dict[str, Any]) -> Path:
    d = profile_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / (_safe_name(name) + ".json")
    data = {"schema": "ableton-mcp/mix-profile", "version": 1, "name": name,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "centres_hz": [round(float(c), 2) for c in thirds.CENTRES],
            "curve_db": [None if not math.isfinite(v) else round(float(v), 2) for v in curve_rel], **meta}
    path.write_text(json.dumps(data, indent=1))
    return path


def load_profile(name: str) -> Dict[str, Any]:
    path = profile_dir() / (_safe_name(name) + ".json")
    if not path.is_file():
        raise ValueError("unknown profile %r; known: %s" % (name, ", ".join(list_profiles()) or "none"))
    data = json.loads(path.read_text())
    if data.get("schema") != "ableton-mcp/mix-profile" or len(data.get("curve_db", [])) != thirds.N_BANDS:
        raise ValueError("profile %r is malformed" % name)
    data["curve"] = np.array([np.nan if v is None else float(v) for v in data["curve_db"]])
    return data


def list_profiles() -> List[str]:
    d = profile_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def target_curve(target: str) -> Dict[str, Any]:
    """Resolve "tilt:-4.5" (built-in slope) or a saved profile name to a curve."""
    m = re.fullmatch(r"\s*tilt\s*:?\s*([-+]?\d+(?:\.\d+)?)\s*", target or "")
    if m:
        slope = float(m.group(1))
        if not -9.0 <= slope <= 3.0:
            raise ValueError("tilt slope must be between -9 and +3 dB/octave")
        return {"name": "tilt %+.1f dB/oct" % slope, "curve": thirds.tilt_curve(slope), "kind": "tilt"}
    data = load_profile(target)
    return {"name": data["name"], "curve": data["curve"], "kind": "profile", "source": data.get("source")}
