"""Tests for the 2.0 command surface (Remote Script 2.0.0).

Live-side: the Remote Script handlers against fakes that mimic the parts of
the Live 12.4.5 API they use. Server-side: the MCP tools with the socket
connection mocked. No Ableton, no network.
"""

import math
import sys
import types

import pytest

import MCP_Server.server as server
from tests.test_remote_script_bridge import _load_script_module, make_instance
from tests.test_returns_and_sends import FakeConnection, call  # noqa: F401
from tests.test_returns_and_sends import _hermetic, fake_conn  # noqa: F401


# ---------------------------------------------------------------- Live fakes

class FakeParam(object):
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0):
        self.name = name
        self.value = value
        self.min = lo
        self.max = hi
        self.is_enabled = True

    def str_for_value(self, v):
        return "%.2f u" % v


class FakeMixer(object):
    def __init__(self):
        self.volume = FakeParam("Track Volume", 0.85)
        self.panning = FakeParam("Track Panning", 0.0, -1.0, 1.0)
        self.sends = [FakeParam("A")]


class FakeDevice(object):
    def __init__(self, name):
        self.name = name
        self.class_name = name.replace(" ", "")
        self.parameters = [FakeParam("Device On", 1.0), FakeParam("Frequency", 0.5)]


class FakeEvent(object):
    def __init__(self, time, value):
        self.time = time
        self.value = value


class FakeEnvelope(object):
    def __init__(self, parameter, events):
        self.parameter = parameter
        self.events = list(events)

    def events_in_range(self, a, b):
        return [FakeEvent(t, v) for t, v in self.events if a <= t <= b]

    def value_at_time(self, t):
        best = self.events[0][1]
        for et, ev in self.events:
            if et <= t:
                best = ev
        return best


class FakeClip(object):
    def __init__(self, name="Clip", length=4.0, midi=True, start_time=0.0):
        self.name = name
        self.length = length
        self.loop_start = 0.0
        self.loop_end = length
        self.start_marker = 0.0
        self.end_marker = length
        self.looping = True
        self.muted = False
        self.launch_mode = 0
        self.launch_quantization = 0
        self.legato = False
        self.color_index = 0
        self.velocity_amount = 0.0
        self.is_midi_clip = midi
        self.is_audio_clip = not midi
        self.start_time = start_time
        self.end_time = start_time + length
        self.envelopes = []
        self.calls = []
        self.notes = []
        if not midi:
            self.gain = 0.5
            self.pitch_coarse = 0
            self.pitch_fine = 0.0
            self.warping = True
            self.warp_mode = 0

    def __setattr__(self, key, value):
        # Live refuses a loop start at/after the loop end (and vice versa).
        if key == "loop_start" and "loop_end" in self.__dict__ and value >= self.loop_end:
            raise RuntimeError("loop_start must be before loop_end")
        if key == "loop_end" and "loop_start" in self.__dict__ and value <= self.loop_start:
            raise RuntimeError("loop_end must be after loop_start")
        object.__setattr__(self, key, value)

    @property
    def has_envelopes(self):
        return bool(self.envelopes)

    @property
    def automation_envelopes(self):
        return list(self.envelopes)

    def automation_envelope(self, param):
        for env in self.envelopes:
            if env.parameter is param:
                return env
        return None

    def quantize(self, grid, amount):
        self.calls.append(("quantize", grid, amount))

    def crop(self):
        self.calls.append(("crop",))

    def duplicate_loop(self):
        self.calls.append(("duplicate_loop",))
        self.length *= 2

    def duplicate_region(self, *args):
        self.calls.append(("duplicate_region",) + args)

    def add_new_notes(self, specs):
        self.notes.extend(specs)


class FakeSlot(object):
    def __init__(self, clip=None):
        self.clip = clip

    @property
    def has_clip(self):
        return self.clip is not None


class FakeTrack(object):
    def __init__(self, name, midi=True):
        self.name = name
        self.mixer_device = FakeMixer()
        self.mute = False
        self.solo = False
        self.arm = False
        self.can_be_armed = True
        self.current_monitoring_state = 1
        self.color_index = 3
        self.has_midi_input = midi
        self.has_audio_input = not midi
        self.devices = []
        self.clip_slots = [FakeSlot(FakeClip("Loop")), FakeSlot()]
        self.arrangement_clips = []

    def create_midi_clip(self, start, length):
        clip = FakeClip("", length, True, start)
        self.arrangement_clips.append(clip)
        self.arrangement_clips.sort(key=lambda c: c.start_time)
        return clip

    def create_audio_clip(self, path, start):
        clip = FakeClip("audio", 8.0, False, start)
        self.arrangement_clips.append(clip)
        return clip

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)

    def duplicate_clip_slot(self, index):
        self.clip_slots[index + 1].clip = FakeClip(self.clip_slots[index].clip.name)
        return index + 1

    def insert_device(self, name, index=-1):
        if name not in ("EQ Eight", "Operator", "Auto Filter"):
            raise RuntimeError("Unknown device")
        device = FakeDevice(name)
        if index == -1:
            self.devices.append(device)
        else:
            self.devices.insert(index, device)
        return device


class FakeScene(object):
    def __init__(self, name):
        self.name = name
        self.tempo = 120.0
        self.tempo_enabled = False
        self.time_signature_numerator = 4
        self.time_signature_denominator = 4
        self.color_index = 0
        self.fired = 0

    def fire(self):
        self.fired += 1


class FakeCue(object):
    def __init__(self, name, time):
        self.name = name
        self.time = time


class FakeSong(object):
    def __init__(self):
        self.tracks = [FakeTrack("1-MIDI"), FakeTrack("2-Audio", midi=False)]
        self.return_tracks = [FakeTrack("A-Reverb", midi=False)]
        self.master_track = FakeTrack("Main", midi=False)
        self.scenes = [FakeScene("1"), FakeScene("2")]
        self.cue_points = [FakeCue("Drop", 16.0)]
        self.undo_log = []
        self.is_playing = False
        self.current_song_time = 0.0
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 16.0
        self.song_length = 64.0
        self.metronome = False
        self.record_mode = False
        self.arrangement_overdub = False
        self.session_record = False
        self.punch_in = False
        self.punch_out = False
        self.clip_trigger_quantization = 4
        self.swing_amount = 0.0
        self.can_undo = True
        self.can_redo = False
        self.can_capture_midi = False
        self.can_jump_to_next_cue = True
        self.can_jump_to_prev_cue = False
        self.root_note = 0
        self.scale_name = "Major"
        self.scale_mode = False

    def __setattr__(self, key, value):
        # The Song-length wall: Live keeps the loop inside the song.
        if key == "loop_length" and "song_length" in self.__dict__:
            value = min(value, self.song_length - self.loop_start)
        object.__setattr__(self, key, value)

    @property
    def scale_intervals(self):
        return {"Major": [0, 2, 4, 5, 7, 9, 11],
                "Minor": [0, 2, 3, 5, 7, 8, 10]}.get(self.scale_name, [0])

    def begin_undo_step(self):
        self.undo_log.append("begin")

    def end_undo_step(self):
        self.undo_log.append("end")

    def undo(self):
        self.undo_log.append("undo")
        self.can_redo = True

    def redo(self):
        self.undo_log.append("redo")

    def delete_track(self, i):
        del self.tracks[i]

    def duplicate_track(self, i):
        self.tracks.insert(i + 1, FakeTrack(self.tracks[i].name + " copy"))

    def delete_return_track(self, i):
        del self.return_tracks[i]

    def create_scene(self, i):
        scene = FakeScene("")
        if i == -1:
            self.scenes.append(scene)
        else:
            self.scenes.insert(i, scene)
        return scene

    def delete_scene(self, i):
        del self.scenes[i]

    def duplicate_scene(self, i):
        self.scenes.insert(i + 1, FakeScene(self.scenes[i].name + " copy"))

    def jump_to_next_cue(self):
        self.current_song_time = 16.0

    def jump_by(self, beats):
        self.current_song_time += beats


@pytest.fixture(scope="module")
def rs():
    return _load_script_module()


@pytest.fixture
def live(rs):
    inst = make_instance(rs)
    inst._song = FakeSong()
    inst.schedule_message = lambda delay, fn: fn()
    return inst


@pytest.fixture
def fake_live_module(monkeypatch):
    """Minimal ``Live`` module: MidiNoteSpecification and the scale list."""
    live_mod = types.ModuleType("Live")
    live_mod.Clip = types.SimpleNamespace(MidiNoteSpecification=lambda **kw: kw)
    live_mod.Song = types.SimpleNamespace(
        get_all_scales_ordered=lambda: (("Major", (0, 2, 4)), ("Minor", (0, 2, 3)),
                                        ("Dorian", (0, 2, 3))))
    monkeypatch.setitem(sys.modules, "Live", live_mod)
    return live_mod


def run(live, command, **params):
    return live._execute_command({"type": command, "params": params})


# ------------------------------------------------------------ pure helpers

def test_capabilities_cover_every_v2_command(rs):
    for name, (handler, _) in rs.V2_COMMANDS.items():
        assert name in rs.SCRIPT_CAPABILITIES
        assert rs.INTEGRATION_COMMANDS[name][0] == handler
        assert hasattr(rs.AbletonMCP, handler)


def test_v2_does_not_shadow_upstream_commands(rs):
    # Upstream owns these; 2.0 must not register its own versions.
    for name in ("set_track_mixer", "get_transport", "set_song_settings",
                 "edit_structure", "stop_all_clips", "jump_to_cue"):
        assert name not in rs.V2_COMMANDS


def test_version_is_2(rs):
    assert rs.SCRIPT_VERSION == "2.0.0+dunk.1"


@pytest.mark.parametrize("value,expected", [
    (0, 0), (11, 11), ("C", 0), ("f#", 6), ("Bb", 10), (" a ", 9), ("Eb", 3)])
def test_parse_root_note(rs, value, expected):
    assert rs.parse_root_note(value) == expected


@pytest.mark.parametrize("value", [12, -1, "H", True, None, 1.5])
def test_parse_root_note_rejects(rs, value):
    with pytest.raises(ValueError):
        rs.parse_root_note(value)


def test_match_scale_name_is_case_insensitive(rs):
    assert rs.match_scale_name("minor pentatonic", rs.DEFAULT_SCALE_NAMES) == "Minor Pentatonic"
    with pytest.raises(ValueError) as exc:
        rs.match_scale_name("Klingon", rs.DEFAULT_SCALE_NAMES)
    assert "Known scales" in str(exc.value)


def test_scale_note_names(rs):
    assert rs.scale_note_names(9, [0, 2, 3, 5, 7, 8, 10]) == [
        "A", "B", "C", "D", "E", "F", "G"]


def test_compress_envelope_events_keeps_jumps_and_drops_holds(rs):
    # A hold at 0.2, a jump to 0.8, a two-step ramp, then a hold.
    events = [(0, 0.2), (1, 0.2), (2, 0.2), (2, 0.8), (3, 0.8), (3, 0.9),
              (3.5, 0.9), (3.5, 1.0), (4, 1.0)]
    assert rs.compress_envelope_events(events) == [
        {"time": 0.0, "value": 0.2}, {"time": 2.0, "value": 0.2},
        {"time": 2.0, "value": 0.8}, {"time": 3.0, "value": 0.8},
        {"time": 3.0, "value": 0.9}, {"time": 3.5, "value": 0.9},
        {"time": 3.5, "value": 1.0}, {"time": 4.0, "value": 1.0}]


def test_lookup_choice(rs):
    assert rs.lookup_choice("launch_mode", "Gate", rs.LAUNCH_MODES) == 1
    assert rs.lookup_choice("launch_mode", 3, rs.LAUNCH_MODES) == 3
    with pytest.raises(ValueError):
        rs.lookup_choice("launch_mode", 9, rs.LAUNCH_MODES)
    with pytest.raises(ValueError):
        rs.lookup_choice("launch_mode", True, rs.LAUNCH_MODES)


# ------------------------------------------------------------ undo grouping

def test_v2_commands_run_inside_one_undo_step(live):
    resp = run(live, "set_scene_properties", scene_index=0, name="Drop")
    assert resp["status"] == "success"
    assert live._song.undo_log == ["begin", "end"]


def test_undo_step_is_closed_when_command_fails(live):
    resp = run(live, "set_scene_properties", scene_index=0, tempo=5000.0)
    assert resp["status"] == "error"
    assert live._song.undo_log == ["begin", "end"]


def test_undo_and_redo_are_not_grouped(live):
    assert run(live, "undo")["status"] == "success"
    assert run(live, "redo")["status"] == "success"
    assert live._song.undo_log == ["undo", "redo"]


def test_undo_refuses_when_nothing_to_undo(live):
    live._song.can_undo = False
    resp = run(live, "undo")
    assert resp["status"] == "error" and "Nothing to undo" in resp["message"]


def test_unknown_command_still_errors(live):
    assert run(live, "make_it_slap")["status"] == "error"


# ------------------------------------------------------------ tracks / scenes

def test_delete_return_track(live):
    result = live._delete_return_track({"return_index": 0})
    assert result["deleted_name"] == "A-Reverb" and result["return_track_count"] == 0
    with pytest.raises(IndexError):
        live._delete_return_track({"return_index": 0})


def test_set_scene_properties(live):
    result = live._set_scene_properties({
        "scene_index": 1, "name": "Chorus", "tempo": 128,
        "tempo_enabled": True, "time_signature_numerator": 7,
        "time_signature_denominator": 8})
    scene = live._song.scenes[1]
    assert (scene.name, scene.tempo, scene.tempo_enabled) == ("Chorus", 128.0, True)
    assert scene.time_signature_numerator == 7
    assert result["changed"][0] == "name"
    with pytest.raises(ValueError):
        live._set_scene_properties({"scene_index": 1, "time_signature_denominator": 3})


# ------------------------------------------------------------ clips

def test_set_clip_properties_loop_moves_past_current_end(live):
    """Setting loop 8-12 on a 0-4 loop must move the end first."""
    live._set_clip_properties({"track_index": 0, "clip_index": 0,
                               "loop_start": 8.0, "loop_end": 12.0,
                               "looping": True, "launch_mode": "toggle",
                               "name": "Hook"})
    clip = live._song.tracks[0].clip_slots[0].clip
    assert (clip.loop_start, clip.loop_end) == (8.0, 12.0)
    assert clip.launch_mode == 2 and clip.name == "Hook"


def test_set_clip_properties_rejects_inverted_loop(live):
    with pytest.raises(ValueError):
        live._set_clip_properties({"track_index": 0, "clip_index": 0,
                                   "loop_start": 3.0, "loop_end": 2.0})


def test_set_clip_properties_audio_only_fields(live):
    with pytest.raises(ValueError) as exc:
        live._set_clip_properties({"track_index": 0, "clip_index": 0, "gain": 0.3})
    assert "audio clips" in str(exc.value)
    live._song.tracks[1].clip_slots[0].clip = FakeClip("Vox", midi=False)
    result = live._set_clip_properties({"track_index": 1, "clip_index": 0,
                                        "gain": 0.3, "warp_mode": "complex pro",
                                        "pitch_coarse": -12})
    assert result["clip"]["warp_mode"] == 6 and result["clip"]["pitch_coarse"] == -12


def test_set_clip_properties_empty_slot(live):
    with pytest.raises(ValueError):
        live._set_clip_properties({"track_index": 0, "clip_index": 1, "muted": True})


def test_edit_clip_actions(live):
    clip = live._song.tracks[0].clip_slots[0].clip
    live._edit_clip({"track_index": 0, "clip_index": 0, "action": "quantize",
                     "grid": "1/8t", "amount": 0.5})
    live._edit_clip({"track_index": 0, "clip_index": 0, "action": "crop"})
    out = live._edit_clip({"track_index": 0, "clip_index": 0, "action": "duplicate_loop"})
    live._edit_clip({"track_index": 0, "clip_index": 0, "action": "duplicate_region",
                     "region_start": 0, "region_length": 2, "destination_time": 4,
                     "transposition": 12})
    assert clip.calls == [("quantize", 3, 0.5), ("crop",), ("duplicate_loop",),
                          ("duplicate_region", 0.0, 2.0, 4.0, -1, 12)]
    assert out["length"] == 8.0
    dup = live._edit_clip({"track_index": 0, "clip_index": 0, "action": "duplicate_slot"})
    assert dup["new_clip_index"] == 1


@pytest.mark.parametrize("params", [
    {"action": "explode"},
    {"action": "quantize", "grid": "1/7"},
    {"action": "quantize", "amount": 2},
    {"action": "duplicate_region", "region_start": 0, "region_length": 0,
     "destination_time": 4},
    {"action": "duplicate_slot", "clip_location": "arrangement"},
])
def test_edit_clip_rejects(live, params):
    live._song.tracks[0].arrangement_clips.append(FakeClip("A"))
    base = {"track_index": 0, "clip_index": 0}
    base.update(params)
    with pytest.raises((ValueError, IndexError)):
        live._edit_clip(base)


# ------------------------------------------------------------ arrangement clips

def test_create_arrangement_midi_clip_with_notes(live, fake_live_module):
    result = live._create_arrangement_clip({
        "track_index": 0, "start_time": 32.0, "length": 8.0, "name": "Verse",
        "notes": [{"pitch": 60, "start_time": 0, "duration": 1}]})
    track = live._song.tracks[0]
    assert result["clip_index"] == 0 and result["start_time"] == 32.0
    assert result["end_time"] == 40.0 and result["note_count"] == 1
    assert track.arrangement_clips[0].name == "Verse"
    assert track.arrangement_clips[0].notes[0]["pitch"] == 60


def test_create_arrangement_clip_validates_notes_before_creating(live, fake_live_module):
    with pytest.raises(ValueError):
        live._create_arrangement_clip({"track_index": 0, "start_time": 0,
                                       "notes": [{"pitch": 200}]})
    assert live._song.tracks[0].arrangement_clips == []


def test_create_arrangement_clip_track_type_checks(live, tmp_path):
    with pytest.raises(ValueError):  # MIDI clip on audio track
        live._create_arrangement_clip({"track_index": 1, "start_time": 0})
    with pytest.raises(ValueError):  # audio on MIDI track
        live._create_arrangement_clip({"track_index": 0, "start_time": 0,
                                       "path": str(tmp_path)})
    with pytest.raises(ValueError):  # relative path
        live._create_arrangement_clip({"track_index": 1, "start_time": 0,
                                       "path": "kick.wav"})
    wav = tmp_path / "kick.wav"
    wav.write_bytes(b"RIFF")
    result = live._create_arrangement_clip({"track_index": 1, "start_time": 4.0,
                                            "path": str(wav)})
    assert result["is_audio_clip"] is True and result["start_time"] == 4.0


def test_delete_arrangement_clip(live, fake_live_module):
    live._create_arrangement_clip({"track_index": 0, "start_time": 0})
    live._create_arrangement_clip({"track_index": 0, "start_time": 8})
    result = live._delete_arrangement_clip({"track_index": 0, "clip_index": 0})
    assert result["start_time"] == 0.0 and result["remaining_clips"] == 1


# ------------------------------------------------------------ automation read-back

def _clip_with_envelopes(live):
    track = live._song.tracks[0]
    track.devices = [FakeDevice("Auto Filter")]
    clip = track.clip_slots[0].clip
    freq = track.devices[0].parameters[1]
    clip.envelopes = [
        FakeEnvelope(freq, [(0, 0.1), (1, 0.1), (1, 0.5), (2, 0.5), (2, 0.9), (4, 0.9)]),
        FakeEnvelope(track.mixer_device.volume, [(0, 0.85), (4, 0.85)]),
    ]
    return clip


def test_get_clip_automation_all_maps_parameters_to_indices(live):
    _clip_with_envelopes(live)
    result = live._get_clip_automation({"track_index": 0, "clip_index": 0})
    assert result["has_envelopes"] is True
    filt, vol = result["envelopes"]
    assert filt["parameter_type"] == "device"
    assert (filt["device_index"], filt["parameter_index"]) == (0, 1)
    assert filt["label"] == "Auto Filter: Frequency"
    assert [p["value"] for p in filt["points"]] == [0.1, 0.1, 0.5, 0.5, 0.9, 0.9]
    assert vol["parameter_type"] == "volume" and len(vol["points"]) == 2


def test_get_clip_automation_single_parameter_with_samples(live):
    _clip_with_envelopes(live)
    result = live._get_clip_automation({
        "track_index": 0, "clip_index": 0, "parameter_type": "device",
        "device_index": 0, "parameter_index": 1, "sample_step": 1.0})
    env = result["envelopes"][0]
    assert [s["value"] for s in env["samples"]] == [0.1, 0.5, 0.9, 0.9, 0.9]


def test_get_clip_automation_without_envelope(live):
    _clip_with_envelopes(live)
    result = live._get_clip_automation({
        "track_index": 0, "clip_index": 0, "parameter_type": "panning"})
    assert result["envelopes"] == [] and result["parameter"] == "Pan"


def test_get_clip_automation_is_a_read_command(live):
    _clip_with_envelopes(live)
    resp = run(live, "get_clip_automation", track_index=0, clip_index=0)
    assert resp["status"] == "success"
    assert live._song.undo_log == []


# ------------------------------------------------------------ insert_device

def test_insert_device_appends_and_inserts(live):
    a = live._insert_device({"track_index": 0, "device_name": "Operator"})
    b = live._insert_device({"track_index": 0, "device_name": "EQ Eight",
                             "device_index": 0})
    assert (a["device_index"], b["device_index"]) == (0, 0)
    assert [d.name for d in live._song.tracks[0].devices] == ["EQ Eight", "Operator"]
    assert b["parameter_count"] == 2


def test_insert_device_unknown_name_gives_hint(live):
    with pytest.raises(RuntimeError) as exc:
        live._insert_device({"track_index": 0, "device_name": "Serum 2"})
    assert "load_instrument_or_effect" in str(exc.value)


def test_insert_device_on_old_live(live):
    class OldTrack(FakeTrack):
        @property
        def insert_device(self):  # Live < 12.3 has no Track.insert_device
            raise AttributeError("insert_device")
    live._song.tracks[0] = OldTrack("old")
    with pytest.raises(RuntimeError) as exc:
        live._insert_device({"track_index": 0, "device_name": "Operator"})
    assert "12.3" in str(exc.value)


def test_insert_device_on_return_and_master(live):
    live._insert_device({"track_type": "return", "track_index": 0,
                         "device_name": "Auto Filter"})
    live._insert_device({"track_type": "master", "device_name": "EQ Eight"})
    assert live._song.return_tracks[0].devices[0].name == "Auto Filter"
    assert live._song.master_track.devices[0].name == "EQ Eight"


# ------------------------------------------------------------ scale

def test_read_scale(live, fake_live_module):
    live._song.root_note = 9
    live._song.scale_name = "Minor"
    result = live._read_scale({"include_available": True})
    assert result["root_name"] == "A" and result["note_names"][:3] == ["A", "B", "C"]
    assert result["available_scales"] == ["Major", "Minor", "Dorian"]


def test_set_scale(live, fake_live_module):
    result = live._set_scale({"root_note": "D", "scale_name": "dorian",
                              "scale_mode": True})
    assert (live._song.root_note, live._song.scale_name, live._song.scale_mode) == (
        2, "Dorian", True)
    assert result["old"]["root_name"] == "C" and result["new"]["root_name"] == "D"


def test_set_scale_rejects_unknown_scale_without_writing(live, fake_live_module):
    with pytest.raises(ValueError):
        live._set_scale({"root_note": "D", "scale_name": "Lydian"})
    assert live._song.root_note == 0


# ------------------------------------------------------------ transport

def test_set_transport_sets_values(live):
    result = live._set_transport({"loop": True, "loop_start": 8.0,
                                  "loop_length": 16.0, "punch_in": True,
                                  "clip_trigger_quantization": 4})
    assert result["warnings"] == []
    assert live._song.loop is True and live._song.punch_in is True
    assert live._song.clip_trigger_quantization == 4


def test_set_transport_points_to_set_song_settings(live):
    with pytest.raises(ValueError) as exc:
        live._set_transport({"metronome": True, "signature_numerator": 3})
    assert "set_song_settings" in str(exc.value)


def test_set_transport_warns_at_song_length_wall(live):
    result = live._set_transport({"loop_start": 56.0, "loop_length": 32.0})
    assert live._song.loop_length == 8.0
    assert "loop_length" in result["warnings"][0]


@pytest.mark.parametrize("params", [{}, {"loop": 1}, {"swing_amount": 0.5},
                                    {"clip_trigger_quantization": 14},
                                    {"loop_length": 0}])
def test_set_transport_rejects(live, params):
    with pytest.raises(ValueError):
        live._set_transport(params)


def test_transport_actions(live):
    assert live._transport_action({"action": "jump_to_next_cue"})["current_song_time"] == 16.0
    assert live._transport_action({"action": "jump_by", "beats": -4})["current_song_time"] == 12.0
    with pytest.raises(ValueError):
        live._transport_action({"action": "jump_to_prev_cue"})
    with pytest.raises(ValueError):
        live._transport_action({"action": "capture_midi"})
    with pytest.raises(ValueError):
        live._transport_action({"action": "rewind_time"})
    with pytest.raises(ValueError):
        live._transport_action({"action": "stop_all_clips"})


# ------------------------------------------------------------ risk fixes

def test_duplicate_to_arrangement_warns_when_automation_is_lost(live):
    track = live._song.tracks[0]
    clip = _clip_with_envelopes(live)
    track.duplicate_clip_to_arrangement = lambda c, t: FakeClip("copy")
    result = live._duplicate_session_clip_to_arrangement(0, 0, 8.0)
    assert result["automation_lost"] is True and "12.4.5" in result["warning"]
    assert clip.has_envelopes

    def keeps(c, t):
        copy = FakeClip("copy")
        copy.envelopes = list(c.envelopes)
        return copy
    track.duplicate_clip_to_arrangement = keeps
    assert "warning" not in live._duplicate_session_clip_to_arrangement(0, 0, 8.0)


# ------------------------------------------------------------ server side

@pytest.mark.parametrize("tool,args,kwargs,command,expected", [
    ("delete_return_track", (None, 1), {}, "delete_return_track", {"return_index": 1}),
    ("set_clip_properties", (None, 0, 1), {"loop_start": 4.0, "loop_end": 8.0},
     "set_clip_properties", {"track_index": 0, "clip_index": 1,
                             "clip_location": "session", "loop_start": 4.0,
                             "loop_end": 8.0}),
    ("create_arrangement_clip", (None, 1, 16.0), {"length": 8.0},
     "create_arrangement_clip", {"track_index": 1, "start_time": 16.0,
                                 "length": 8.0}),
    ("insert_device", (None, 0, "EQ Eight"), {}, "insert_device",
     {"track_index": 0, "device_name": "EQ Eight", "device_index": -1,
      "track_type": "track"}),
    ("set_scale", (None,), {"root_note": "7", "scale_name": "Minor"}, "set_scale",
     {"root_note": 7, "scale_name": "Minor"}),
    ("set_transport", (None,), {"loop": True, "loop_length": 16.0}, "set_transport",
     {"loop": True, "loop_length": 16.0}),
    ("undo", (None,), {}, "undo", {}),
])
def test_server_tools_forward_only_set_params(fake_conn, tool, args, kwargs,
                                              command, expected):
    conn = fake_conn({})
    call(getattr(server, tool), *args, **kwargs)
    assert conn.sent == [(command, expected)]


def test_server_get_clip_automation_returns_json(fake_conn):
    fake_conn({"envelopes": [{"label": "Volume", "points": []}]})
    out = call(server.get_clip_automation, None, 0, 0)
    assert '"label": "Volume"' in out


def test_server_set_transport_shows_warnings(fake_conn):
    fake_conn({"changed": ["loop_length"], "state": {"loop": True},
               "warnings": ["loop_length is 8.0, not 32.0"]})
    out = call(server.set_transport, None, loop_length=32.0)
    assert "Warning: loop_length is 8.0" in out


def test_server_duplicate_to_arrangement_shows_warning(fake_conn):
    fake_conn({"clip_name": "Loop", "track_name": "Bass",
               "warning": "automation lost"})
    out = call(server.duplicate_to_arrangement, None, 0, 0, 8.0)
    assert "Warning: automation lost" in out


def test_server_errors_become_tool_errors(fake_conn):
    fake_conn(raise_exc=server.AbletonCommandError("Unknown device"))
    with pytest.raises(server.ToolError) as exc:
        call(server.insert_device, None, 0, "Serum")
    assert "Unknown device" in str(exc.value)


def test_server_and_script_agree_on_modifying_commands(rs):
    assert server.V2_MODIFYING_COMMANDS == {
        name for name, (_, modifying) in rs.V2_COMMANDS.items() if modifying}


def test_every_v2_command_has_a_server_tool(rs):
    import asyncio
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(names) == len(set(names))
    for command in rs.V2_COMMANDS:
        assert command in names


def test_dataset_tables_know_the_new_tools():
    from MCP_Server.dataset.schema import ACTION_CATEGORIES
    from MCP_Server.dataset.trajectory_decorator import MODIFYING_TOOLS
    for tool in ("create_arrangement_clip", "insert_device",
                 "set_scale", "edit_clip", "set_clip_properties"):
        assert tool in ACTION_CATEGORIES and tool in MODIFYING_TOOLS
    assert math.isfinite(len(ACTION_CATEGORIES))
