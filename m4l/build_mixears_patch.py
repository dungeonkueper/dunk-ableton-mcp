"""Generate MixEars.maxpat, the Max for Live analyzer patch, as plain JSON.

    python m4l/build_mixears_patch.py            # writes m4l/MixEars.maxpat

The patch is written by code (not by hand in Max) so it can be reviewed and
regenerated. See docs/mix-ears.md for turning it into a .amxd device.

Signal flow (audio passes through unchanged):

    plugin~ -> plugout~
       L,R  -> average~ rms (rmsL, rmsR), peakamp~ (peakL, peakR)
       mid=(L+R)/2, side=(L-R)/2 -> average~ rms
       mid  -> fffb~ 31 bands (1/3 octave, Q 4.318) -> average~ rms each
    metro 50 -> t b b: right outlet bangs every snapshot~/peakamp~ into the
       cold inlets of one pack, left outlet then bangs the pack
    pack -> prepend /mixears/frame <track name> -> udpsend 127.0.0.1 9881

The track name comes from live.path "this_device canonical_parent" and a
live.observer on its name, so renaming the track renames the source.
"""

from __future__ import annotations

import json
from pathlib import Path

N_BANDS = 31
BASE_HZ = 1000.0 * 2.0 ** (-17 / 3.0)
RATIO = 2.0 ** (1.0 / 3.0)
Q = 1.0 / (2.0 ** (1.0 / 6.0) - 2.0 ** (-1.0 / 6.0))
WINDOW = 2400       # samples per RMS window (50 ms at 48 kHz)
INTERVAL_MS = 50
PORT = 9881
ADDRESS = "/mixears/frame"

# (inlets, outlets, outlet types) for objects we use; Max recomputes these on load.
IO = {
    "plugin~": (1, 2, ["signal", "signal"]),
    "plugout~": (2, 0, []),
    "average~": (1, 1, ["signal"]),
    "snapshot~": (2, 1, [""]),
    "peakamp~": (2, 1, [""]),
    "+~": (2, 1, ["signal"]),
    "-~": (2, 1, ["signal"]),
    "*~": (2, 1, ["signal"]),
    "fffb~": (1, N_BANDS, ["signal"] * N_BANDS),
    "metro": (2, 1, ["bang"]),
    "t": (1, 2, ["bang", "bang"]),
    "pack": (38, 1, [""]),
    "prepend": (1, 1, [""]),
    "udpsend": (1, 0, []),
    "live.thisdevice": (1, 3, ["bang", "int", "int"]),
    "live.path": (1, 3, ["", "", ""]),
    "live.observer": (2, 2, ["", ""]),
    "route": (1, 2, ["", ""]),
    "zl": (2, 2, ["", ""]),
}


class Patch(object):
    def __init__(self):
        self.boxes, self.lines, self.n = [], [], 0

    def obj(self, text, x, y, w=None, maxclass="newobj", **extra):
        self.n += 1
        oid = "obj-%d" % self.n
        head = text.split()[0] if maxclass == "newobj" else maxclass
        ins, outs, types = IO.get(head, (1, 1, [""]))
        if head == "t":
            outs = len(text.split()) - 1
            types = ["bang"] * outs
        if head == "pack":
            ins = len(text.split()) - 1
        box = {"id": oid, "maxclass": maxclass, "numinlets": ins, "numoutlets": outs,
               "outlettype": types, "patching_rect": [x, y, w or max(40, 7 * len(text) + 12), 22.0]}
        if maxclass in ("newobj", "message", "comment"):
            box["text"] = text
        box.update(extra)
        self.boxes.append({"box": box})
        return oid

    def msg(self, text, x, y):
        return self.obj(text, x, y, maxclass="message")

    def comment(self, text, x, y, w=300, **extra):
        return self.obj(text, x, y, w=w, maxclass="comment", **extra)

    def connect(self, a, ao, b, bi):
        self.lines.append({"patchline": {"source": [a, ao], "destination": [b, bi]}})

    def to_json(self):
        return {"patcher": {
            "fileversion": 1,
            "appversion": {"major": 8, "minor": 6, "revision": 0, "architecture": "x64", "modernui": 1},
            "classnamespace": "box",
            "rect": [80.0, 80.0, 1500.0, 900.0],
            "openinpresentation": 1,
            "default_fontsize": 12.0,
            "gridsize": [15.0, 15.0],
            "boxes": self.boxes,
            "lines": self.lines,
            "dependency_cache": [],
            "autosave": 0,
        }}


def build():
    p = Patch()
    p.comment("MixEars: sends levels + 31 third-octave bands to the ableton-mcp server "
              "(UDP %d, OSC %s). Audio passes through unchanged." % (PORT, ADDRESS), 20, 10, w=900)
    title = p.comment("MixEars -> ableton-mcp  (UDP %d)" % PORT, 20, 40, w=260,
                      presentation=1, presentation_rect=[8.0, 8.0, 260.0, 22.0])
    p.comment("Measures the mid signal of this track; source = track name.", 20, 60, w=400,
              presentation=1, presentation_rect=[8.0, 32.0, 330.0, 22.0])
    _ = title

    inp = p.obj("plugin~", 20, 100)
    out = p.obj("plugout~", 20, 140)
    p.connect(inp, 0, out, 0)
    p.connect(inp, 1, out, 1)

    # clock: metro -> t b b (right: bang meters, left: bang pack)
    dev = p.obj("live.thisdevice", 300, 100)
    start = p.msg("1", 300, 130)
    metro = p.obj("metro %d" % INTERVAL_MS, 300, 160)
    trig = p.obj("t b b", 300, 190)
    p.connect(dev, 0, start, 0)
    p.connect(start, 0, metro, 0)
    p.connect(metro, 0, trig, 0)

    # Slot 0 is a dummy: the hot inlet only ever receives the bang, so the
    # list goes out once per tick, after every meter has updated its slot.
    pack = p.obj("pack " + " ".join(["0."] * (7 + N_BANDS)), 20, 700, w=1400)
    p.connect(trig, 0, pack, 0)  # left outlet fires last: output the list

    def meter(sig_src, sig_outlet, inlet, x, y, kind="rms"):
        if kind == "rms":
            avg = p.obj("average~ %d rms" % WINDOW, x, y)
            snap = p.obj("snapshot~", x, y + 30)
            p.connect(sig_src, sig_outlet, avg, 0)
            p.connect(avg, 0, snap, 0)
        else:
            snap = p.obj("peakamp~", x, y + 30)
            p.connect(sig_src, sig_outlet, snap, 0)
        p.connect(trig, 1, snap, 0)
        p.connect(snap, 0, pack, inlet)

    meter(inp, 0, 1, 20, 250)              # rms L
    meter(inp, 1, 2, 120, 250)             # rms R
    meter(inp, 0, 3, 220, 250, "peak")     # peak L
    meter(inp, 1, 4, 320, 250, "peak")     # peak R
    add = p.obj("+~", 440, 180)
    sub = p.obj("-~", 520, 180)
    p.connect(inp, 0, add, 0); p.connect(inp, 1, add, 1)
    p.connect(inp, 0, sub, 0); p.connect(inp, 1, sub, 1)
    mid = p.obj("*~ 0.5", 440, 210)
    side = p.obj("*~ 0.5", 520, 210)
    p.connect(add, 0, mid, 0); p.connect(sub, 0, side, 0)
    meter(mid, 0, 5, 420, 250)             # rms mid
    meter(side, 0, 6, 520, 250)            # rms side

    bank = p.obj("fffb~ %d %.4f %.6f %.4f" % (N_BANDS, BASE_HZ, RATIO, Q), 620, 360, w=780)
    p.connect(mid, 0, bank, 0)
    for k in range(N_BANDS):
        meter(bank, k, 7 + k, 620 + (k % 16) * 50, 420 + (k // 16) * 90)

    # Drop the dummy slot, prefix address + track name, send.
    strip = p.obj("zl slice 1", 20, 740)
    p.connect(pack, 0, strip, 0)
    pre = p.obj("prepend %s untitled" % ADDRESS, 20, 770)
    p.connect(strip, 1, pre, 0)
    udp = p.obj("udpsend 127.0.0.1 %d" % PORT, 20, 800)
    p.connect(pre, 0, udp, 0)

    # Track name -> "set /mixears/frame <name>" into prepend.
    t2 = p.obj("t b b", 700, 100)
    p.connect(dev, 0, t2, 0)
    path_msg = p.msg("path this_device canonical_parent", 700, 130)
    lpath = p.obj("live.path", 700, 160)
    obs = p.obj("live.observer name", 700, 190)
    route = p.obj("route name", 700, 220)
    setmsg = p.msg("set %s $1" % ADDRESS, 700, 250)
    p.connect(t2, 1, path_msg, 0)
    p.connect(path_msg, 0, lpath, 0)
    p.connect(lpath, 0, obs, 1)       # "id N" into the observer's right inlet
    p.connect(obs, 0, route, 0)
    p.connect(route, 0, setmsg, 0)
    p.connect(route, 1, setmsg, 0)    # observers may output the bare value
    p.connect(setmsg, 0, pre, 0)
    return p.to_json()


def main():
    out = Path(__file__).with_name("MixEars.maxpat")
    out.write_text(json.dumps(build(), indent=1) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
