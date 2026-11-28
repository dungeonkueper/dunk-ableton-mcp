"""MCP tools for live mix listening (Max for Live analyzer feed) and mix diagnosis.

Measure -> act -> measure again:
  get_mix_analysis / compare_to_reference / get_masking_report  (measure)
  apply_eq_suggestions                                          (act, one undo step)
  get_mix_analysis(against="before")                            (measure again)

All analysis needs the optional ``analysis`` extra (numpy, scipy, soundfile).
Nothing here installs software or touches files except reading audio files
the caller names and writing reference profiles to the local cache.
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, Image
from mcp.server.fastmcp.exceptions import ToolError

from .dataset.trajectory_decorator import trajectory_tool
from .telemetry_decorator import rich_telemetry_tool

MAX_LISTEN_S = 30.0
_state: Dict[str, Any] = {"feed": None, "snapshots": {}}


def _ears():
    try:
        from .ears import mixdoctor, mixfeed, spectrogram, thirds
    except ImportError as error:
        raise ToolError("Install ableton-mcp[analysis] (numpy, scipy, soundfile) to use mix analysis") from error
    return mixfeed, mixdoctor, thirds, spectrogram


def get_feed():
    mixfeed = _ears()[0]
    if _state["feed"] is None:
        _state["feed"] = mixfeed.MixFeed()
    feed = _state["feed"]
    if not feed.listening:
        try:
            feed.start()
        except OSError as error:
            raise ToolError("Could not open UDP port %s for the analyzer feed: %s" % (mixfeed.DEFAULT_PORT, error))
    return feed


def _clean(value: Any) -> Any:
    """numpy -> plain JSON (NaN -> None)."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None
    if np is not None and isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, float):
        return None if not math.isfinite(value) else round(value, 2)
    if np is not None and isinstance(value, np.generic):
        return _clean(value.item())
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


NO_FEED = ("No analyzer frames from %s. Is the MixEars Max for Live device on that track, is Live playing, "
           "and does the device send to UDP port %s? Sources heard so far: %s. "
           "Fallback without Max for Live: record_audio + analyze_audio_file / compare_to_reference(mix_path=...).")


async def _listen(feed, source: Optional[str], seconds: float) -> tuple:
    seconds = float(min(max(seconds, 0.5), MAX_LISTEN_S))
    start = feed.clock()
    await asyncio.sleep(seconds)
    name = feed.resolve(source)
    frames = feed.window(name, seconds, since=start) if name else []
    if len(frames) < 3:
        raise ToolError(NO_FEED % (repr(source) if source else "the master (or a single source)",
                                   feed.port, ", ".join(feed.sources()) or "none"))
    return name, frames


def _flags(s: Dict[str, Any]) -> List[str]:
    out = []
    if s["peak_dbfs"] is not None and s["peak_dbfs"] > -1.0:
        out.append("peaks above -1 dBFS: risk of clipping / inter-sample overs")
    if s["correlation_approx"] is not None and s["correlation_approx"] < 0:
        out.append("negative correlation: phase problems, check in mono")
    if s["crest_db"] is not None and s["crest_db"] < 6:
        out.append("crest factor below 6 dB: heavily limited / squashed")
    if s["balance_db"] is not None and abs(s["balance_db"]) > 1.5:
        out.append("left/right imbalance of %+.1f dB (positive = right louder)" % s["balance_db"])
    return out


def _public_summary(s: Dict[str, Any], thirds) -> Dict[str, Any]:
    out = {k: v for k, v in s.items() if k not in ("band_power", "band_db", "relative_curve")}
    out["band_level_db"] = thirds.curve_dict(s["band_db"])
    out["relative_curve_db"] = thirds.curve_dict(s["relative_curve"])
    out["flags"] = _flags(s)
    return out


def _diagnose(mix_rel, target: Dict[str, Any], mixdoctor, thirds, max_moves: int) -> Dict[str, Any]:
    dev = mixdoctor.deviation(mix_rel, target["curve"])
    moves = mixdoctor.suggest_eq(dev, max_moves=max_moves)
    return {"target": target["name"], "deviation_db": thirds.curve_dict(dev),
            "eq_suggestions": moves,
            "note": ("Curves are loudness-independent (each relative to its own total). Moves correct ~60 % of the "
                     "smoothed deviation; pass them to apply_eq_suggestions, then measure again.")}


def _file_curve(path: str, start_s: Optional[float], end_s: Optional[float]):
    from .ears import load_audio
    from .ears import thirds
    from .ears.loudness import measure
    x, sr = load_audio(path, start_s, end_s)
    power = thirds.band_powers(x.mean(axis=1), sr)
    loud = measure(x, sr)
    valid = thirds.CENTRES * thirds.EDGE < sr / 2.0 * 0.98
    return thirds.relative_curve(power, valid), loud, x, sr


def register_mix_tools(mcp, get_connection, registry=None):

    @mcp.tool(name="get_mix_feed_status")
    @rich_telemetry_tool("get_mix_feed_status")
    async def get_mix_feed_status(ctx: Context, user_prompt: str = "") -> str:
        """Status of the live analyzer feed: UDP port, which tracks send frames, saved reference profiles.

        Starts the local listener (127.0.0.1 only) if it is not running yet.
        """
        _, mixdoctor, _, _ = _ears()
        feed = get_feed()
        return json.dumps({"listening": feed.listening, "port": feed.port, "sources": feed.sources(),
                           "stats": feed.stats, "snapshots": sorted(_state["snapshots"]),
                           "profiles": mixdoctor.list_profiles()}, allow_nan=False)

    @mcp.tool(name="get_mix_analysis")
    @rich_telemetry_tool("get_mix_analysis")
    @trajectory_tool("get_mix_analysis", modifying=False)
    async def get_mix_analysis(ctx: Context, source: str = "", seconds: float = 4.0, target: str = "",
                               snapshot_as: str = "", against: str = "", max_moves: int = 4,
                               user_prompt: str = "") -> str:
        """Listen to the live mix for a few seconds (MixEars Max for Live device) and report what it hears.

        Live must be playing. Returns peak/RMS dBFS, crest factor, approximate
        LUFS (from the analyzer's levels and spectrum, typically within ~1 LU),
        L/R balance, approximate correlation, the 31-band 1/3-octave levels,
        a loudness-independent relative curve and warning flags.

        Parameters:
        - source: track name the device sits on; empty = "Master"/"Main" (or the only source)
        - seconds: how long to listen, 0.5..30 (play a representative section!)
        - target: optional "tilt:-2" (slope in dB/oct of band power; pink noise = 0)
          or a profile saved with save_reference_profile -> adds deviation + EQ suggestions
        - snapshot_as: remember this measurement under a label (e.g. "before")
        - against: compare with an earlier snapshot label -> per-band change in dB
        """
        mixfeed, mixdoctor, thirds, _ = _ears()
        feed = get_feed()
        name, frames = await _listen(feed, source or None, seconds)
        s = mixfeed.summarize(frames)
        out = {"source": name, **_public_summary(s, thirds)}
        if target:
            try:
                out["diagnosis"] = _diagnose(s["relative_curve"], mixdoctor.target_curve(target), mixdoctor, thirds, max_moves)
            except ValueError as error:
                raise ToolError(str(error)) from error
        if against:
            prev = _state["snapshots"].get(against)
            if prev is None:
                raise ToolError("No snapshot %r; known: %s" % (against, ", ".join(_state["snapshots"]) or "none"))
            change = s["relative_curve"] - prev["relative_curve"]
            out["change_vs_" + against] = {
                "relative_curve_db": thirds.curve_dict(change),
                "loudness_lu": None if prev["loudness_lufs_approx"] is None or s["loudness_lufs_approx"] is None
                else round(s["loudness_lufs_approx"] - prev["loudness_lufs_approx"], 1),
                "peak_db": None if prev["peak_dbfs"] is None or s["peak_dbfs"] is None
                else round(s["peak_dbfs"] - prev["peak_dbfs"], 1),
            }
        if snapshot_as:
            _state["snapshots"][snapshot_as] = s
        return json.dumps(_clean(out), allow_nan=False)

    @mcp.tool(name="compare_to_reference")
    @rich_telemetry_tool("compare_to_reference")
    @trajectory_tool("compare_to_reference", modifying=False)
    async def compare_to_reference(ctx: Context, reference_path: str = "", profile: str = "", source: str = "",
                                   seconds: float = 6.0, mix_path: str = "", ref_start_s: float = None,
                                   ref_end_s: float = None, max_moves: int = 4, user_prompt: str = "") -> str:
        """Compare the mix's tonal balance with a reference track (file) or a saved profile and suggest EQ moves.

        The mix comes from the live analyzer feed (listens `seconds` while Live
        plays) or, if mix_path is given, from an audio file (e.g. record_audio
        output or a bounce). Reference files are analysed with the same
        1/3-octave filter bank as the Max for Live device; at most 120 s are
        read (use ref_start_s/ref_end_s to pick the drop/chorus).
        Returns per-band deviation (mix minus reference, dB), loudness of both,
        and up to max_moves EQ suggestions for apply_eq_suggestions.
        """
        mixfeed, mixdoctor, thirds, _ = _ears()
        if bool(reference_path) == bool(profile):
            raise ToolError("Give exactly one of reference_path or profile")
        try:
            if reference_path:
                ref_curve, ref_loud, _, _ = await asyncio.to_thread(_file_curve, reference_path, ref_start_s, ref_end_s)
                target = {"name": reference_path, "curve": ref_curve}
            else:
                target, ref_loud = mixdoctor.target_curve(profile), None
        except ValueError as error:
            raise ToolError(str(error)) from error
        if mix_path:
            try:
                mix_curve, mix_loud, _, _ = await asyncio.to_thread(_file_curve, mix_path, None, None)
            except ValueError as error:
                raise ToolError(str(error)) from error
            mix_info = {"mix": mix_path, "mix_loudness_lufs": mix_loud.get("integrated_lufs")}
        else:
            name, frames = await _listen(get_feed(), source or None, seconds)
            s = mixfeed.summarize(frames)
            mix_curve = s["relative_curve"]
            mix_info = {"mix": "live:" + name, "mix_loudness_lufs_approx": s["loudness_lufs_approx"],
                        "mix_flags": _flags(s)}
        out = dict(mix_info)
        if ref_loud is not None:
            out["reference_loudness_lufs"] = ref_loud.get("integrated_lufs")
        out.update(_diagnose(mix_curve, target, mixdoctor, thirds, max_moves))
        return json.dumps(_clean(out), allow_nan=False)

    @mcp.tool(name="save_reference_profile")
    @rich_telemetry_tool("save_reference_profile")
    async def save_reference_profile(ctx: Context, name: str, path: str, start_s: float = None,
                                     end_s: float = None, user_prompt: str = "") -> str:
        """Save a reference track's 1/3-octave curve as a named target (e.g. "techno-ref", "my-house-sound").

        Stored locally (~/.cache/ableton-mcp/mix-profiles/<name>.json); only
        the 31-band curve and loudness are kept, never audio. Use the name as
        target in get_mix_analysis or profile in compare_to_reference.
        """
        _, mixdoctor, thirds, _ = _ears()
        try:
            curve, loud, _, sr = await asyncio.to_thread(_file_curve, path, start_s, end_s)
            saved = mixdoctor.save_profile(name, curve, {"source": path, "start_s": start_s, "end_s": end_s,
                                                         "sample_rate": sr,
                                                         "loudness_lufs": loud.get("integrated_lufs")})
        except ValueError as error:
            raise ToolError(str(error)) from error
        return json.dumps(_clean({"saved": str(saved), "curve_db": thirds.curve_dict(curve),
                                  "loudness_lufs": loud.get("integrated_lufs")}), allow_nan=False)

    @mcp.tool(name="get_masking_report")
    @rich_telemetry_tool("get_masking_report")
    @trajectory_tool("get_masking_report", modifying=False)
    async def get_masking_report(ctx: Context, seconds: float = 4.0, exclude: List[str] = None,
                                 user_prompt: str = "") -> str:
        """Find tracks that compete in the same frequency range (needs MixEars on each track to check).

        Listens `seconds` while Live plays, then reports pairs whose levels are
        within 6 dB of each other in contested 1/3-octave bands (classic: kick
        vs bass at 60-120 Hz), with a carving/sidechain hint. Master/Main is
        excluded by default.
        """
        mixfeed, mixdoctor, thirds, _ = _ears()
        feed = get_feed()
        skip = {e.lower() for e in (exclude if exclude is not None else ["Master", "Main"])}
        seconds = float(min(max(seconds, 0.5), MAX_LISTEN_S))
        start = feed.clock()
        await asyncio.sleep(seconds)
        levels = {}
        for name in feed.sources():
            if name.lower() in skip:
                continue
            frames = feed.window(name, seconds, since=start)
            if len(frames) >= 3:
                levels[name] = mixfeed.summarize(frames)["band_power"]
        if len(levels) < 2:
            raise ToolError("Masking needs live frames from at least two tracks (heard: %s)" % (", ".join(levels) or "none"))
        return json.dumps(_clean({"tracks": sorted(levels), "conflicts": mixdoctor.masking(levels)}), allow_nan=False)

    @mcp.tool(name="apply_eq_suggestions")
    @rich_telemetry_tool("apply_eq_suggestions")
    @trajectory_tool("apply_eq_suggestions")
    async def apply_eq_suggestions(ctx: Context, track_index: int, moves: List[Dict[str, Any]],
                                   track_type: str = "track", device_index: int = 0,
                                   device_path: List[int] = None, bands: List[int] = None,
                                   dry_run: bool = False, user_prompt: str = "") -> str:
        """Write EQ moves (from get_mix_analysis / compare_to_reference) into an EQ Eight, as one undo step.

        The device at device_index (or device_path) must already be an EQ Eight
        (load it first with load_instrument_or_effect). Each move needs type
        (bell | low_shelf | high_shelf | low_cut), freq_hz, gain_db and q.
        Bands: low cut/low shelf -> band 1, high shelf -> band 8, bells -> 2..7
        by frequency, unless `bands` gives explicit band numbers. Only the
        listed bands are changed. dry_run=True shows the plan. Measure again
        afterwards; Ctrl/Cmd+Z in Live reverts the whole batch.
        For track_type "master" use track_index 0.
        """
        _, mixdoctor, _, _ = _ears()
        from .device_registry import RegistryClient, RegistryError, jsonable
        from .registry_tools import _registry
        from .script_handshake import require_capability
        get_connection()
        for command in ("scan_device", "set_device_parameters"):
            missing = require_capability(command)
            if missing:
                raise ToolError(missing)
        if not moves or len(moves) > 8:
            raise ToolError("Give 1..8 moves")
        clean = []
        for m in moves:
            if not isinstance(m, dict) or m.get("type") not in mixdoctor.FILTER_TYPE_PATTERNS:
                raise ToolError("Each move needs type bell|low_shelf|high_shelf|low_cut: %r" % (m,))
            try:
                c = {"type": m["type"], "freq_hz": float(m["freq_hz"]), "gain_db": float(m.get("gain_db", 0.0)),
                     "q": float(m.get("q", 0.71))}
            except (KeyError, TypeError, ValueError) as error:
                raise ToolError("Each move needs numeric freq_hz, gain_db and q: %r" % (m,)) from error
            if not (20.0 <= c["freq_hz"] <= 20000.0 and abs(c["gain_db"]) <= 15.0 and 0.1 <= c["q"] <= 18.0):
                raise ToolError("Move out of range (20 Hz..20 kHz, +/-15 dB, Q 0.1..18): %r" % (m,))
            clean.append(c)
        if bands is not None and (len(bands) != len(clean) or len(set(bands)) != len(bands)
                                  or not all(isinstance(b, int) and 1 <= b <= 8 for b in bands)):
            raise ToolError("bands must give one distinct EQ Eight band 1..8 per move")
        path = list(device_path) if device_path else [device_index]
        client = RegistryClient(get_connection().send_command, registry or _registry)

        def run():
            entry, _, _ = client.ensure(track_type, track_index, path)
            if entry["class_name"] != "Eq8":
                raise RegistryError("device is %s, not an EQ Eight (Eq8); load EQ Eight first" % entry["class_name"])
            type_param = next((p for p in entry["parameters"] if p["name"] == "1 Filter Type A"), None)
            if type_param is None or not type_param.get("items"):
                raise RegistryError("EQ Eight scan has no '1 Filter Type A' items")
            changes = mixdoctor.eq8_changes(clean, type_param["items"], bands)
            result = client.set_parameters(track_type, track_index, path, changes, dry_run)
            result["moves"] = clean
            return result
        try:
            return json.dumps(jsonable(await asyncio.to_thread(run)), allow_nan=False)
        except (RegistryError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(name="get_mix_spectrogram")
    @rich_telemetry_tool("get_mix_spectrogram")
    async def get_mix_spectrogram(ctx: Context, source: str = "", seconds: float = 20.0, path: str = "",
                                  start_s: float = None, end_s: float = None, user_prompt: str = "") -> Image:
        """Spectrogram image (low frequencies at the bottom, grid at 100 Hz / 1 kHz / 10 kHz).

        Without path: the last `seconds` (max 120) of the live feed for source
        (31 1/3-octave rows, already recorded frames, no waiting). With path:
        an STFT of an audio file (max 120 s). Look for constant bright lines
        (resonances), a dense smear at 200-500 Hz (mud) or missing top end.
        """
        mixfeed, _, _, spectrogram = _ears()
        try:
            if path:
                from .ears import load_audio
                x, sr = await asyncio.to_thread(load_audio, path, start_s, end_s)
                png = await asyncio.to_thread(spectrogram.from_audio, x, sr)
            else:
                feed = get_feed()
                name = feed.resolve(source or None)
                frames = feed.window(name, min(max(seconds, 1.0), 120.0)) if name else []
                if len(frames) < 2:
                    raise ToolError(NO_FEED % (repr(source) if source else "the master", feed.port,
                                               ", ".join(feed.sources()) or "none"))
                import numpy as np
                png = await asyncio.to_thread(spectrogram.from_bands, np.array([f.bands for f in frames]))
        except ValueError as error:
            raise ToolError(str(error)) from error
        return Image(data=png, format="png")
