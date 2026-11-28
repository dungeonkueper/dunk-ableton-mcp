"""Live analyzer feed: receive meter frames from Max for Live over UDP (OSC).

Wire format (one OSC message per frame, ~20 per second per device)::

    /mixears/frame  ,s f f f f f f f*31
        source          track name the device sits on (e.g. "Master", "Bass")
        rms_l rms_r     channel RMS over the last frame (linear)
        peak_l peak_r   channel peak since the last frame (linear)
        rms_mid rms_side  RMS of (L+R)/2 and (L-R)/2
        band[0..30]     RMS of the mid signal in 31 1/3-octave bands (see thirds.py)

Integers are accepted wherever floats are. The listener binds to 127.0.0.1
only. Frames are kept per source in a ring buffer (default 120 s).
"""

from __future__ import annotations

import math
import os
import socket
import struct
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from . import thirds

ADDRESS = "/mixears/frame"
DEFAULT_PORT = int(os.environ.get("ABLETON_MCP_MIX_PORT", "9881"))
N_LEVELS = 6
N_ARGS = 1 + N_LEVELS + thirds.N_BANDS
HISTORY_S = 120.0


# ------------------------------------------------------------------ OSC

def _pad4(n: int) -> int:
    return (n + 4) & ~3


def _read_string(data: bytes, pos: int) -> Tuple[str, int]:
    end = data.index(b"\0", pos)
    return data[pos:end].decode("utf-8", "replace"), pos + _pad4(end - pos)


def decode_osc(data: bytes) -> Tuple[str, List[Any]]:
    """Decode one OSC message (bundles are unpacked recursively by decode_packet)."""
    address, pos = _read_string(data, 0)
    if pos >= len(data):
        return address, []
    tags, pos = _read_string(data, pos)
    if not tags.startswith(","):
        raise ValueError("OSC message without type tags")
    args: List[Any] = []
    for tag in tags[1:]:
        if tag == "f":
            args.append(struct.unpack(">f", data[pos:pos + 4])[0]); pos += 4
        elif tag == "i":
            args.append(struct.unpack(">i", data[pos:pos + 4])[0]); pos += 4
        elif tag == "d":
            args.append(struct.unpack(">d", data[pos:pos + 8])[0]); pos += 8
        elif tag == "h":
            args.append(struct.unpack(">q", data[pos:pos + 8])[0]); pos += 8
        elif tag == "s":
            text, pos = _read_string(data, pos); args.append(text)
        elif tag in "TF":
            args.append(tag == "T")
        elif tag == "N":
            args.append(None)
        else:
            raise ValueError("unsupported OSC type tag %r" % tag)
    return address, args


def decode_packet(data: bytes) -> List[Tuple[str, List[Any]]]:
    if data.startswith(b"#bundle\0"):
        out, pos = [], 16
        while pos + 4 <= len(data):
            size = struct.unpack(">i", data[pos:pos + 4])[0]
            out += decode_packet(data[pos + 4:pos + 4 + size])
            pos += 4 + size
        return out
    return [decode_osc(data)]


def encode_osc(address: str, args: List[Any]) -> bytes:
    """Minimal OSC encoder (used by the simulator and tests)."""
    def s(text: str) -> bytes:
        raw = text.encode("utf-8") + b"\0"
        return raw + b"\0" * (-len(raw) % 4)
    tags, payload = ",", b""
    for a in args:
        if isinstance(a, str):
            tags += "s"; payload += s(a)
        elif isinstance(a, int) and not isinstance(a, bool):
            tags += "i"; payload += struct.pack(">i", a)
        else:
            tags += "f"; payload += struct.pack(">f", float(a))
    return s(address) + s(tags) + payload


# ------------------------------------------------------------------ store

class Frame(object):
    __slots__ = ("t", "levels", "bands")

    def __init__(self, t: float, levels: np.ndarray, bands: np.ndarray):
        self.t, self.levels, self.bands = t, levels, bands


def parse_frame(args: List[Any]) -> Tuple[str, np.ndarray, np.ndarray]:
    if len(args) != N_ARGS or not isinstance(args[0], str):
        raise ValueError("expected %d arguments (source, 6 levels, 31 bands), got %d" % (N_ARGS, len(args)))
    values = np.array([float(a) for a in args[1:]], dtype=np.float64)
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        raise ValueError("levels must be finite and non-negative")
    return args[0].strip() or "?", values[:N_LEVELS], values[N_LEVELS:]


class MixFeed(object):
    """Thread-safe frame store plus an optional UDP listener."""

    def __init__(self, history_s: float = HISTORY_S, clock=time.monotonic):
        self.history_s = history_s
        self.clock = clock
        self._frames: Dict[str, Deque[Frame]] = {}
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None
        self.stats = {"packets": 0, "frames": 0, "rejected": 0, "last_error": None}

    # -- ingest
    def add(self, source: str, levels: np.ndarray, bands: np.ndarray, t: Optional[float] = None) -> None:
        t = self.clock() if t is None else t
        with self._lock:
            q = self._frames.setdefault(source, deque())
            q.append(Frame(t, levels, bands))
            while q and q[0].t < t - self.history_s:
                q.popleft()
        self.stats["frames"] += 1

    def ingest_packet(self, data: bytes, t: Optional[float] = None) -> int:
        self.stats["packets"] += 1
        added = 0
        try:
            for address, args in decode_packet(data):
                if address != ADDRESS:
                    continue
                source, levels, bands = parse_frame(args)
                self.add(source, levels, bands, t)
                added += 1
        except Exception as error:  # malformed packets are counted, never raised
            self.stats["rejected"] += 1
            self.stats["last_error"] = str(error)[:200]
        return added

    # -- listener
    def start(self, port: Optional[int] = None, host: str = "127.0.0.1") -> int:
        if self._thread is not None and self._thread.is_alive():
            return self.port  # type: ignore[return-value]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, DEFAULT_PORT if port is None else port))
        sock.settimeout(0.5)
        self._sock, self.port = sock, sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="mixears-udp", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    @property
    def listening(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while self._sock is not None:
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            self.ingest_packet(data)

    # -- queries
    def sources(self) -> Dict[str, Dict[str, Any]]:
        now = self.clock()
        with self._lock:
            return {name: {"frames": len(q), "age_s": round(now - q[-1].t, 2) if q else None,
                           "span_s": round(q[-1].t - q[0].t, 2) if len(q) > 1 else 0.0}
                    for name, q in self._frames.items()}

    def window(self, source: str, seconds: float, since: Optional[float] = None) -> List[Frame]:
        now = self.clock()
        start = now - seconds if since is None else since
        with self._lock:
            q = self._frames.get(source)
            if q is None:
                key = next((k for k in self._frames if k.lower() == source.lower()), None)
                q = self._frames.get(key) if key else None
            return [f for f in (q or ()) if f.t >= start]

    def resolve(self, source: Optional[str]) -> Optional[str]:
        names = list(self._frames)
        if source:
            return next((k for k in names if k.lower() == source.lower()), source)
        for preferred in ("master", "main"):
            for k in names:
                if k.lower() == preferred:
                    return k
        return names[0] if len(names) == 1 else None

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()


# ------------------------------------------------------------------ summaries

def _db(power: float) -> Optional[float]:
    return None if power <= 1e-20 else round(10.0 * math.log10(power), 1)


def _gated_lufs(blocks: List[float]) -> Optional[float]:
    """BS.1770-style gating over 400 ms block loudness values."""
    vals = np.array([b for b in blocks if b is not None and b > -70.0])
    if vals.size == 0:
        return None
    rel = 10.0 * math.log10(np.mean(10.0 ** (vals / 10.0))) - 10.0
    kept = vals[vals > rel]
    return round(10.0 * math.log10(np.mean(10.0 ** (kept / 10.0))), 1) if kept.size else None


LIVE_SR = float(os.environ.get("ABLETON_MCP_MIX_SR", "48000"))


def summarize(frames: List[Frame], sr: float = LIVE_SR) -> Dict[str, Any]:
    """Average a window of frames into levels, approximate loudness and a band curve.

    ``sr`` is the sample rate Live runs at (only used to undo the filter
    bank's warping near Nyquist; ABLETON_MCP_MIX_SR sets the default).
    """
    if not frames:
        raise ValueError("no frames in this window")
    lv = np.array([f.levels for f in frames])
    bands = np.array([f.bands for f in frames]) ** 2 * thirds.bank_correction(sr)  # power per frame and band
    ms_l, ms_r = float(np.mean(lv[:, 0] ** 2)), float(np.mean(lv[:, 1] ** 2))
    ms_m, ms_s = float(np.mean(lv[:, 4] ** 2)), float(np.mean(lv[:, 5] ** 2))
    peak = float(np.max(lv[:, 2:4]))
    rms = math.sqrt((ms_l + ms_r) / 2.0)
    band_power = np.mean(bands, axis=0)

    def loud(i0: int, i1: int) -> Optional[float]:
        # K-weighted power of the mid signal from its spectrum, scaled to the
        # L+R channel sum (assumes the side signal has a similar spectrum).
        chans = float(np.mean(lv[i0:i1, 0] ** 2 + lv[i0:i1, 1] ** 2))
        mid = float(np.mean(lv[i0:i1, 4] ** 2))
        if mid <= 1e-20:
            return None
        p = thirds.k_weighted_power(np.mean(bands[i0:i1], axis=0)) * chans / mid
        return None if p <= 1e-20 else -0.691 + 10.0 * math.log10(p)

    # 400 ms blocks with 75 % overlap, by timestamp.
    t = np.array([f.t for f in frames])
    blocks = []
    start = t[0]
    while start + 0.4 <= t[-1] + 1e-9:
        idx = np.nonzero((t >= start) & (t < start + 0.4))[0]
        if idx.size:
            blocks.append(loud(int(idx[0]), int(idx[-1]) + 1))
        start += 0.1
    short = np.nonzero(t >= t[-1] - 3.0)[0]
    corr = None if ms_m + ms_s <= 1e-20 else round((ms_m - ms_s) / (ms_m + ms_s), 2)
    crest = None if rms <= 1e-10 or peak <= 0 else round(20.0 * math.log10(peak / rms), 1)
    rel = thirds.relative_curve(band_power)
    return {
        "frames": len(frames),
        "span_s": round(float(t[-1] - t[0]), 2),
        "peak_dbfs": None if peak <= 0 else round(20.0 * math.log10(peak), 1),
        "rms_dbfs": _db(rms * rms),
        "crest_db": crest,
        "loudness_lufs_approx": _gated_lufs(blocks) if blocks else (None if loud(0, len(frames)) is None else round(loud(0, len(frames)), 1)),
        "short_term_lufs_approx": None if loud(int(short[0]), len(frames)) is None else round(loud(int(short[0]), len(frames)), 1),
        "balance_db": None if ms_l <= 1e-20 or ms_r <= 1e-20 else round(10.0 * math.log10(ms_r / ms_l), 1),
        "correlation_approx": corr,
        "side_to_mid_db": None if ms_m <= 1e-20 or ms_s <= 1e-20 else round(10.0 * math.log10(ms_s / ms_m), 1),
        "band_power": band_power,
        "band_db": thirds.to_db(band_power),
        "relative_curve": rel,
    }
