"""Software stand-in for the MixEars Max for Live device.

Computes the same frames as the device (levels + 31 1/3-octave band RMS of
the mid signal) from an audio file and sends them over UDP in real time.
Use it to test the server pipeline without Max, or to feed a bounce.

    python -m MCP_Server.ears.mixsim mix.wav --source Master
    python -m MCP_Server.ears.mixsim bass.wav --source Bass &  python -m MCP_Server.ears.mixsim kick.wav --source Kick

Only reads the file; sends to 127.0.0.1 by default.
"""

from __future__ import annotations

import argparse
import socket
import time
from typing import Iterator, Tuple

import numpy as np

from . import mixfeed, thirds

FRAME_S = 0.05


def frames_from_audio(x: np.ndarray, sr: float, frame_s: float = FRAME_S) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Yield (levels[6], band_rms[31]) per frame, exactly like the M4L device."""
    x = np.asarray(x, float)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    left, right = x[:, 0], x[:, 1]
    bank = thirds.FilterBank(sr)
    n = max(1, int(round(frame_s * sr)))
    for i in range(0, left.size - n + 1, n):
        l, r = left[i:i + n], right[i:i + n]
        mid, side = (l + r) / 2.0, (l - r) / 2.0
        levels = np.array([np.sqrt(np.mean(l * l)), np.sqrt(np.mean(r * r)),
                           np.max(np.abs(l)), np.max(np.abs(r)),
                           np.sqrt(np.mean(mid * mid)), np.sqrt(np.mean(side * side))])
        yield levels, bank.process(mid)


def packet(source: str, levels: np.ndarray, bands: np.ndarray) -> bytes:
    return mixfeed.encode_osc(mixfeed.ADDRESS, [source] + [float(v) for v in levels] + [float(v) for v in bands])


def main(argv=None) -> None:
    from .audio import load_audio

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path")
    ap.add_argument("--source", default="Master")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=mixfeed.DEFAULT_PORT)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--fast", action="store_true", help="do not wait between frames")
    args = ap.parse_args(argv)
    x, sr = load_audio(args.path, args.start, args.end)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        t0, k = time.monotonic(), 0
        for levels, bands in frames_from_audio(x, sr):
            sock.sendto(packet(args.source, levels, bands), (args.host, args.port))
            k += 1
            if not args.fast:
                delay = t0 + k * FRAME_S - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        if not args.loop:
            break


if __name__ == "__main__":
    main()
