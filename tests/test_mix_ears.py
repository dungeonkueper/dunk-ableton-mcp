"""Live mix listening: OSC feed, 1/3-octave analysis, EQ suggestions, masking, EQ Eight writes.

All signals are synthetic and Live is faked; nothing here needs Max or Live.
"""
import asyncio
import json
import socket
import struct
import time
import types
import zlib

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("soundfile")
import soundfile as sf

from MCP_Server.ears import mixdoctor, mixfeed, mixsim, spectrogram, thirds
from MCP_Server.ears.loudness import measure
from tests.test_ears import SR, peaking_eq, pink_noise, sine


def stereo(x):
    return np.stack([x, x], axis=1)


def feed_with(signals, t0=0.0):
    """MixFeed filled from {source: stereo signal} through the device-equivalent simulator."""
    feed = mixfeed.MixFeed(clock=lambda: 1e9)
    for name, x in signals.items():
        t = t0
        for levels, bands in mixsim.frames_from_audio(x, SR):
            feed.add(name, levels, bands, t)
            t += mixsim.FRAME_S
    return feed


def all_frames(feed, name):
    return feed.window(name, 1e12, since=-1.0)


def hp_pink(n, seed=0):
    from scipy import signal
    sos = signal.butter(2, 12, btype="high", fs=SR, output="sos")
    return signal.sosfilt(sos, pink_noise(n, seed=seed))


# ------------------------------------------------------------------ OSC + UDP

def test_osc_roundtrip_bundle_and_rejects():
    pkt = mixsim.packet("Bass Line", np.arange(6) / 10.0, np.ones(31) * 0.5)
    address, args = mixfeed.decode_osc(pkt)
    assert address == mixfeed.ADDRESS and args[0] == "Bass Line" and len(args) == mixfeed.N_ARGS
    bundle = b"#bundle\0" + b"\0" * 8 + struct.pack(">i", len(pkt)) + pkt
    feed = mixfeed.MixFeed()
    assert feed.ingest_packet(bundle) == 1
    assert feed.ingest_packet(mixfeed.encode_osc(mixfeed.ADDRESS, ["x", 1.0])) == 0
    assert feed.ingest_packet(b"garbage") == 0
    assert feed.stats["rejected"] == 2 and "Bass Line" in feed.sources()
    assert feed.ingest_packet(mixfeed.encode_osc("/other", [1])) == 0  # ignored, not rejected
    assert feed.stats["rejected"] == 2


def test_udp_listener_receives_frames_on_localhost():
    feed = mixfeed.MixFeed()
    port = feed.start(port=0)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for levels, bands in mixsim.frames_from_audio(stereo(sine(1000, 0.5, amp=0.5)), SR):
            sock.sendto(mixsim.packet("Master", levels, bands), ("127.0.0.1", port))
        deadline = time.time() + 3
        while time.time() < deadline and feed.sources().get("Master", {}).get("frames", 0) < 10:
            time.sleep(0.02)
        assert feed.sources()["Master"]["frames"] == 10
        assert feed.resolve(None) == "Master" and feed.resolve("master") == "Master"
    finally:
        feed.stop()
    assert not feed.listening


def test_history_is_bounded():
    feed = mixfeed.MixFeed(history_s=1.0)
    for k in range(100):
        feed.add("A", np.zeros(6), np.zeros(31), t=k * 0.05)
    assert feed.sources  # no error
    assert len(feed.window("A", 1e9, since=-1)) <= 21


# ------------------------------------------------------------------ analysis

def test_pink_noise_is_flat_and_sine_lands_in_its_band():
    rel = thirds.relative_curve(thirds.band_powers(hp_pink(SR * 6), SR))
    assert np.nanmax(rel[2:]) - np.nanmin(rel[2:]) < 1.5
    tone = thirds.band_powers(sine(1000, 2.0, amp=0.5), SR)
    assert int(np.argmax(tone)) == 17 and thirds.NOMINAL[17] == "1k"
    # unity gain at the centre: band power of a 0.5 sine is about 0.125 (times the bank correction)
    assert abs(10 * np.log10(tone[17] / (0.125 * thirds.bank_correction(SR)[17]))) < 0.3


def test_live_summary_levels_and_loudness_match_offline_measurement():
    rng = np.random.default_rng(4)
    left = hp_pink(SR * 8, seed=1) * 0.6 + 0.2 * sine(55, 8.0)
    right = 0.8 * left + 0.2 * hp_pink(SR * 8, seed=2) * 0.6
    x = np.stack([left, right], axis=1) + rng.normal(scale=1e-4, size=(SR * 8, 2))
    s = mixfeed.summarize(all_frames(feed_with({"Master": x}), "Master"), sr=SR)
    truth = measure(x, SR)
    assert abs(s["loudness_lufs_approx"] - truth["integrated_lufs"]) < 1.0
    assert abs(s["peak_dbfs"] - truth["sample_peak_dbfs"]) < 0.1
    assert abs(s["rms_dbfs"] - truth["rms_dbfs"]) < 0.3
    assert 0.5 < s["correlation_approx"] < 1.0
    mono = mixfeed.summarize(all_frames(feed_with({"M": stereo(sine(440, 2, amp=0.5))}), "M"), sr=SR)
    assert mono["correlation_approx"] == 1.0 and mono["balance_db"] == 0.0


def test_live_curve_equals_file_curve():
    x = peaking_eq(hp_pink(SR * 6, seed=3), 3000, 5, 1.2)
    live = mixfeed.summarize(all_frames(feed_with({"Master": stereo(x)}), "Master"), sr=SR)["relative_curve"]
    offline = thirds.relative_curve(thirds.band_powers(x, SR))
    assert np.nanmax(np.abs(live[1:] - offline[1:])) < 0.5


def test_eq_suggestion_finds_a_boost_and_cuts_it():
    ref = hp_pink(SR * 6, seed=5)
    mix = peaking_eq(ref, 250, 6, 1.0)
    dev = mixdoctor.deviation(thirds.relative_curve(thirds.band_powers(mix, SR)),
                              thirds.relative_curve(thirds.band_powers(ref, SR)))
    moves = mixdoctor.suggest_eq(dev)
    assert len(moves) == 1
    m = moves[0]
    assert m["type"] == "bell" and 200 <= m["freq_hz"] <= 315 and -4.5 < m["gain_db"] < -2.0
    assert 0.6 <= m["q"] <= 1.5 and "mud" in m["reason"]
    # applying the suggested correction brings the curve closer to the reference
    fixed = peaking_eq(mix, m["freq_hz"], m["gain_db"], m["q"])
    dev2 = mixdoctor.deviation(thirds.relative_curve(thirds.band_powers(fixed, SR)),
                               thirds.relative_curve(thirds.band_powers(ref, SR)))
    assert np.nanmax(np.abs(dev2)) < np.nanmax(np.abs(dev)) * 0.6


def test_eq_suggestions_shelves_cuts_and_flat():
    flat = np.zeros(31)
    assert mixdoctor.suggest_eq(flat) == []
    dull = np.zeros(31)
    dull[25:] = -4.0  # too little top end
    moves = mixdoctor.suggest_eq(dull)
    assert moves[-1]["type"] == "high_shelf" and moves[-1]["gain_db"] > 0
    rumble = np.zeros(31)
    rumble[:3] = 8.0
    moves = mixdoctor.suggest_eq(rumble)
    assert moves[0]["type"] == "low_cut" and moves[0]["freq_hz"] < 60
    many = np.tile([4.0, 4.0, 0.0, -4.0, -4.0, 0.0], 6)[:31]
    assert len(mixdoctor.suggest_eq(many, max_moves=3)) == 3


def test_masking_finds_kick_vs_bass():
    from scipy import signal
    n = SR * 4
    rng = np.random.default_rng(7)
    low = signal.sosfilt(signal.butter(4, [60, 120], btype="band", fs=SR, output="sos"), rng.normal(size=n)) * 0.5
    kick = low + 0.02 * signal.sosfilt(signal.butter(2, 2000, btype="high", fs=SR, output="sos"), rng.normal(size=n))
    bass = 0.9 * signal.sosfilt(signal.butter(4, [60, 120], btype="band", fs=SR, output="sos"), rng.normal(size=n)) * 0.5
    hats = signal.sosfilt(signal.butter(4, 8000, btype="high", fs=SR, output="sos"), rng.normal(size=n)) * 0.2
    feed = feed_with({"Kick": stereo(kick), "Bass": stereo(bass), "Hats": stereo(hats)})
    levels = {name: mixfeed.summarize(all_frames(feed, name), sr=SR)["band_power"] for name in ("Kick", "Bass", "Hats")}
    conflicts = mixdoctor.masking(levels)
    assert conflicts and set(conflicts[0]["pair"]) == {"Kick", "Bass"}
    assert 50 <= conflicts[0]["focus_hz"] <= 130 and conflicts[0]["zone"] == "bass"
    assert not any("Hats" in c["pair"] and c["zone"] == "bass" for c in conflicts)


def test_profiles_and_tilt_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_PROFILE_DIR", str(tmp_path))
    curve = thirds.tilt_curve(-2.0)
    assert abs((curve[20] - curve[17]) - (-2.0)) < 0.01  # 3 bands = 1 octave
    mixdoctor.save_profile("My Techno Ref!", curve, {"source": "x.wav"})
    assert mixdoctor.list_profiles() == ["my-techno-ref"]
    t = mixdoctor.target_curve("My Techno Ref!")
    assert np.allclose(t["curve"], curve, atol=0.01)
    assert mixdoctor.target_curve("tilt:-3")["kind"] == "tilt"
    with pytest.raises(ValueError):
        mixdoctor.target_curve("tilt:-20")
    with pytest.raises(ValueError, match="unknown profile"):
        mixdoctor.target_curve("nope")


def test_spectrogram_png_is_valid():
    png = spectrogram.from_bands(np.abs(np.random.default_rng(0).normal(size=(40, 31))) + 1e-3, width=120, height=62)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (120, 62)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == h * (1 + 3 * w)
    png2 = spectrogram.from_audio(stereo(sine(1000, 1.0)), SR, width=100, height=50)
    assert struct.unpack(">II", png2[16:24]) == (100, 50)


# ------------------------------------------------------------------ EQ Eight

EQ_TYPES = ["Low Cut 48", "Low Cut 12", "Low Shelf", "Bell", "Notch", "High Shelf", "High Cut 12", "High Cut 48"]


def test_eq8_changes_mapping():
    moves = [{"type": "low_cut", "freq_hz": 35, "gain_db": 0, "q": 0.71},
             {"type": "bell", "freq_hz": 250, "gain_db": -3, "q": 1.4},
             {"type": "bell", "freq_hz": 3150, "gain_db": 1.5, "q": 2},
             {"type": "high_shelf", "freq_hz": 8000, "gain_db": 2, "q": 0.71}]
    changes = mixdoctor.eq8_changes(moves, EQ_TYPES)
    assert changes["1 Filter Type A"] == "Low Cut 12" and "1 Gain A" not in changes
    assert changes["2 Filter Type A"] == "Bell" and changes["2 Frequency A"] == "250 Hz" and changes["2 Gain A"] == "-3.0 dB"
    assert changes["3 Frequency A"] == "3.15 kHz" and changes["3 Resonance A"] == "2.00"
    assert changes["8 Filter Type A"] == "High Shelf" and changes["8 Filter On A"] is True
    with pytest.raises(ValueError):
        mixdoctor.filter_type_item(["A", "B"], "bell")


def fake_eq8():
    from tests.test_device_registry import FakeParam

    def freq(v):
        f = 10 * 2200 ** v
        return ("%.2f kHz" % (f / 1000)) if f >= 1000 else "%.1f Hz" % f

    params = [FakeParam("Device On", 1, 0, 1, items=["Off", "On"])]
    for n in range(1, 9):
        params += [
            FakeParam("%d Filter On A" % n, 0, 0, 1, items=["Off", "On"]),
            FakeParam("%d Filter Type A" % n, 3, 0, 7, items=EQ_TYPES),
            FakeParam("%d Frequency A" % n, 0.5, fmt=freq),
            FakeParam("%d Gain A" % n, 0.5, fmt=lambda v: "%.2f dB" % (-15 + 30 * v)),
            FakeParam("%d Resonance A" % n, 0.71, 0.1, 18.0, fmt=lambda v: "%.2f" % v),
        ]
    return types.SimpleNamespace(name="EQ Eight", class_name="Eq8", class_display_name="EQ Eight",
                                 can_have_chains=False, parameters=params)


def _tools(live_env, monkeypatch):
    from MCP_Server import mix_tools, script_handshake
    registered = {}

    class FakeMCP:
        def tool(self, name=None, **_):
            def deco(fn):
                registered[name or fn.__name__] = fn
                return fn
            return deco

    conn = types.SimpleNamespace(send_command=live_env.send)
    monkeypatch.setattr(script_handshake, "require_capability", lambda command: None)
    mix_tools.register_mix_tools(FakeMCP(), lambda: conn, registry=live_env.registry)
    return registered


def test_apply_eq_suggestions_writes_one_batch(monkeypatch, tmp_path):
    from tests.test_device_registry import make_live
    env = make_live(monkeypatch, tmp_path, device=fake_eq8())
    tools = _tools(env, monkeypatch)
    moves = [{"type": "bell", "freq_hz": 250, "gain_db": -3.0, "q": 1.4},
             {"type": "high_shelf", "freq_hz": 8000, "gain_db": 2.0, "q": 0.71}]
    dry = json.loads(asyncio.run(tools["apply_eq_suggestions"](None, track_index=0, moves=moves, dry_run=True)))
    assert dry["dry_run"] and env.device.parameters[6].value == 0  # "2 Filter On A"
    out = json.loads(asyncio.run(tools["apply_eq_suggestions"](None, track_index=0, moves=moves)))
    p = {q.name: q for q in env.device.parameters}
    assert p["2 Filter On A"].value == 1 and p["2 Filter Type A"].value == 3
    assert p["2 Frequency A"].str_for_value(p["2 Frequency A"].value) == "250.0 Hz"
    assert p["2 Gain A"].str_for_value(p["2 Gain A"].value) == "-3.00 dB"
    assert abs(p["2 Resonance A"].value - 1.4) < 0.01
    assert p["8 Filter Type A"].value == 5 and p["8 Filter On A"].value == 1
    assert p["3 Filter On A"].value == 0  # untouched
    assert env.calls.count("set_device_parameters") == 1 and out["device"] == "Eq8"
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="range"):
        asyncio.run(tools["apply_eq_suggestions"](None, track_index=0, moves=[{"type": "bell", "freq_hz": 5, "gain_db": 0, "q": 1}]))
    with pytest.raises(ToolError, match="type"):
        asyncio.run(tools["apply_eq_suggestions"](None, track_index=0, moves=[{"type": "comb", "freq_hz": 500}]))


def test_apply_eq_refuses_other_devices(monkeypatch, tmp_path):
    from tests.test_device_registry import make_live
    from mcp.server.fastmcp.exceptions import ToolError
    env = make_live(monkeypatch, tmp_path)
    tools = _tools(env, monkeypatch)
    with pytest.raises(ToolError, match="not an EQ Eight"):
        asyncio.run(tools["apply_eq_suggestions"](None, track_index=0, moves=[{"type": "bell", "freq_hz": 500, "gain_db": -2, "q": 1}]))
    assert "set_device_parameters" not in env.calls


# ------------------------------------------------------------------ tools end to end

def test_tools_listen_compare_and_snapshot(monkeypatch, tmp_path):
    from MCP_Server import mix_tools
    monkeypatch.setenv("ABLETON_MCP_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(mixfeed, "LIVE_SR", float(SR))
    ref = hp_pink(SR * 6, seed=8) * 0.4
    mix = peaking_eq(ref, 250, 6, 1.0)
    ref_path, mix_path = tmp_path / "ref.wav", tmp_path / "mix.wav"
    sf.write(ref_path, stereo(ref), SR)
    sf.write(mix_path, stereo(mix), SR)

    feed = mixfeed.MixFeed()
    port = feed.start(port=0)
    monkeypatch.setitem(mix_tools._state, "feed", feed)
    monkeypatch.setitem(mix_tools._state, "snapshots", {})
    tools = _tools(types.SimpleNamespace(send=None, registry=None), monkeypatch)

    def play(x, seconds):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        frames = list(mixsim.frames_from_audio(stereo(x), SR))[: int(seconds / mixsim.FRAME_S)]
        t0 = time.monotonic()
        for k, (lv, b) in enumerate(frames):
            sock.sendto(mixsim.packet("Master", lv, b), ("127.0.0.1", port))
            delay = t0 + k * 0.01 - time.monotonic()  # 5x faster than real time
            if delay > 0:
                time.sleep(delay)

    async def listen_while_playing(coro, x):
        task = asyncio.create_task(coro)
        await asyncio.sleep(0.05)
        await asyncio.to_thread(play, x, 4.0)
        return await task

    try:
        before = json.loads(asyncio.run(listen_while_playing(
            tools["get_mix_analysis"](None, seconds=1.0, snapshot_as="before", target="tilt:0"), mix)))
        assert before["source"] == "Master" and before["frames"] >= 20
        moves = before["diagnosis"]["eq_suggestions"]
        assert any(m["type"] == "bell" and 160 <= m["freq_hz"] <= 400 and m["gain_db"] < 0 for m in moves)

        cmp_live = json.loads(asyncio.run(listen_while_playing(
            tools["compare_to_reference"](None, reference_path=str(ref_path), seconds=1.0), mix)))
        assert cmp_live["mix"] == "live:Master" and cmp_live["eq_suggestions"]
        cmp_file = json.loads(asyncio.run(tools["compare_to_reference"](None, reference_path=str(ref_path), mix_path=str(mix_path))))
        m = cmp_file["eq_suggestions"][0]
        assert 200 <= m["freq_hz"] <= 315 and m["gain_db"] < 0

        saved = json.loads(asyncio.run(tools["save_reference_profile"](None, name="ref", path=str(ref_path))))
        assert saved["saved"].endswith("ref.json")
        fixed = peaking_eq(mix, m["freq_hz"], m["gain_db"], m["q"])
        after = json.loads(asyncio.run(listen_while_playing(
            tools["get_mix_analysis"](None, seconds=1.0, against="before", target="ref"), fixed)))
        assert after["change_vs_before"]["relative_curve_db"]["250"] < -1.0
        status = json.loads(asyncio.run(tools["get_mix_feed_status"](None)))
        assert status["profiles"] == ["ref"] and status["snapshots"] == ["before"]
        img = asyncio.run(tools["get_mix_spectrogram"](None, seconds=60))
        assert img.data[:4] == b"\x89PNG"
    finally:
        feed.stop()


def test_no_feed_gives_actionable_error(monkeypatch):
    from MCP_Server import mix_tools
    from mcp.server.fastmcp.exceptions import ToolError
    feed = mixfeed.MixFeed()
    feed.start(port=0)
    monkeypatch.setitem(mix_tools._state, "feed", feed)
    tools = _tools(types.SimpleNamespace(send=None, registry=None), monkeypatch)
    try:
        with pytest.raises(ToolError, match="MixEars"):
            asyncio.run(tools["get_mix_analysis"](None, seconds=0.5))
    finally:
        feed.stop()


def test_max_patch_generator_is_consistent(tmp_path):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("gen", pathlib.Path(__file__).parent.parent / "m4l" / "build_mixears_patch.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    patch = gen.build()["patcher"]
    boxes = {b["box"]["id"]: b["box"] for b in patch["boxes"]}
    texts = [b.get("text", "") for b in boxes.values()]
    assert any(t.startswith("fffb~ 31 19.68") for t in texts)
    assert "udpsend 127.0.0.1 %d" % mixfeed.DEFAULT_PORT in texts
    assert any(t == "prepend %s untitled" % mixfeed.ADDRESS for t in texts)
    pack = next(b for b in boxes.values() if b.get("text", "").startswith("pack "))
    assert pack["numinlets"] == 1 + mixfeed.N_LEVELS + thirds.N_BANDS
    for line in patch["lines"]:
        src, so = line["patchline"]["source"]
        dst, di = line["patchline"]["destination"]
        assert so < boxes[src]["numoutlets"] and di < boxes[dst]["numinlets"]
    # every pack value inlet 1..37 is fed exactly once
    fed = sorted(l["patchline"]["destination"][1] for l in patch["lines"]
                 if l["patchline"]["destination"][0] == pack["id"] and l["patchline"]["destination"][1] > 0)
    assert fed == list(range(1, 1 + mixfeed.N_LEVELS + thirds.N_BANDS))
    committed = json.loads((pathlib.Path(__file__).parent.parent / "m4l" / "MixEars.maxpat").read_text())
    assert committed == json.loads(json.dumps(gen.build()))
