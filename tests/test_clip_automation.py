"""Tests for set_clip_automation and clear_clip_automation.

Live-side: the pure step builder, and the Remote Script methods against fake
clips/envelopes that mimic the Live API (Clip.automation_envelope returns
None until create_automation_envelope is called; AutomationEnvelope only
offers insert_step and value_at_time). Server-side: the MCP tools with the
socket connection mocked.
"""

import math

import pytest

import MCP_Server.server as server
from tests.test_remote_script_bridge import _load_script_module, make_instance
from tests.test_returns_and_sends import (  # noqa: F401  (autouse fixture)
    FakeParam, _hermetic, call, fake_conn,
)


# ---------------------------------------------------------------- Live fakes

class FakeEnvelope(object):
    def __init__(self):
        self.steps = []

    def insert_step(self, time, duration, value):
        self.steps.append((time, duration, value))

    def value_at_time(self, time):
        value = None
        for t, d, v in self.steps:
            if t <= time + 1e-9:
                value = v
        return value


class FakeClip(object):
    def __init__(self, name="Clip", length=8.0):
        self.name = name
        self.length = length
        self.envelopes = {}
        self.cleared_all = False

    def automation_envelope(self, param):
        return self.envelopes.get(id(param))

    def create_automation_envelope(self, param):
        env = FakeEnvelope()
        self.envelopes[id(param)] = env
        return env

    def clear_envelope(self, param):
        self.envelopes.pop(id(param), None)

    def clear_all_envelopes(self):
        self.envelopes.clear()
        self.cleared_all = True


class FakeSlot(object):
    def __init__(self, clip=None):
        self.clip = clip
        self.has_clip = clip is not None


class FakeMixer(object):
    def __init__(self):
        self.volume = FakeParam("Volume", 0.85)
        self.panning = FakeParam("Pan", 0.0, -1.0, 1.0)
        self.sends = [FakeParam("A"), FakeParam("B")]


class FakeDevice(object):
    def __init__(self):
        self.name = "Auto Filter"
        self.parameters = [FakeParam("Device On", 1.0),
                           FakeParam("Frequency", 0.5, 20.0, 18000.0)]


class FakeTrack(object):
    def __init__(self):
        self.name = "Bass"
        self.mixer_device = FakeMixer()
        self.devices = [FakeDevice()]
        self.clip_slots = [FakeSlot(FakeClip("Loop", 8.0)), FakeSlot()]
        self.arrangement_clips = [FakeClip("Arr 1", 16.0)]


class FakeSong(object):
    def __init__(self):
        self.tracks = [FakeTrack()]


@pytest.fixture(scope="module")
def rs():
    return _load_script_module()


@pytest.fixture
def live(rs):
    inst = make_instance(rs)
    inst._song = FakeSong()
    return inst


def freq(live):
    return live._song.tracks[0].devices[0].parameters[1]


def env_of(clip, param):
    return clip.envelopes[id(param)]


# ---------------------------------------------------------- step builder

def test_capabilities_advertised(rs):
    assert "set_clip_automation" in rs.SCRIPT_CAPABILITIES
    assert "clear_clip_automation" in rs.SCRIPT_CAPABILITIES


def test_ramp_is_stepped_and_held_to_clip_end(rs):
    steps = rs.build_automation_steps(
        [{"time": 0, "value": 0.0}, {"time": 1, "value": 1.0}],
        clip_length=4.0, lo=0.0, hi=1.0, resolution=0.25)
    assert [s[0] for s in steps] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert [round(s[2], 6) for s in steps] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert steps[-1] == (1.0, 3.0, 1.0)  # final value held to the end
    assert all(math.isclose(s[1], 0.25) for s in steps[:-1])


def test_uneven_span_shortens_last_ramp_step(rs):
    steps = rs.build_automation_steps(
        [{"time": 0, "value": 0.0}, {"time": 0.6, "value": 0.6}],
        clip_length=0.6, lo=0.0, hi=1.0, resolution=0.25)
    assert [round(s[0], 6) for s in steps] == [0.0, 0.25, 0.5]
    assert math.isclose(steps[-1][1], 0.1)


def test_equal_times_make_a_jump(rs):
    steps = rs.build_automation_steps(
        [{"time": 0, "value": 0.2}, {"time": 2, "value": 0.2},
         {"time": 2, "value": 0.9}],
        clip_length=4.0, lo=0.0, hi=1.0, resolution=1.0)
    assert steps == [(0.0, 1.0, 0.2), (1.0, 1.0, 0.2), (2.0, 2.0, 0.9)]


def test_single_point_is_constant(rs):
    assert rs.build_automation_steps([{"time": 0, "value": 0.5}], 8.0, 0, 1) \
        == [(0.0, 8.0, 0.5)]


@pytest.mark.parametrize("points", [
    [],
    "x",
    [{"time": 0}],
    [{"time": -1, "value": 0.5}],
    [{"time": 9, "value": 0.5}],
    [{"time": 0, "value": 1.5}],
    [{"time": 0, "value": float("nan")}],
    [{"time": True, "value": 0.5}],
    [{"time": 2, "value": 0.5}, {"time": 1, "value": 0.5}],
    [5],
])
def test_bad_points_rejected(rs, points):
    with pytest.raises(ValueError):
        rs.build_automation_steps(points, 8.0, 0.0, 1.0)


@pytest.mark.parametrize("resolution", [0, 0.001, -1, float("inf"), "0.25"])
def test_bad_resolution_rejected(rs, resolution):
    with pytest.raises(ValueError):
        rs.build_automation_steps([{"time": 0, "value": 0}], 8.0, 0, 1,
                                  resolution)


def test_step_limit(rs):
    with pytest.raises(ValueError):
        rs.build_automation_steps(
            [{"time": 0, "value": 0}, {"time": 1000, "value": 1}],
            1000.0, 0, 1, 0.03125)


# ---------------------------------------------------------- Remote Script

def test_filter_sweep_on_session_clip(live):
    pts = [{"time": 0, "value": 200.0}, {"time": 8, "value": 12000.0}]
    res = live._set_clip_automation(0, 0, pts, "device", 0, 1)
    clip = live._song.tracks[0].clip_slots[0].clip
    env = env_of(clip, freq(live))
    assert len(env.steps) == 32 and res["step_count"] == 32
    assert env.steps[0] == (0.0, 0.25, 200.0)
    assert res["parameter"] == "Auto Filter: Frequency"
    assert res["check"] == [{"time": 0.0, "value": 200.0}]  # 8.0 = clip end


def test_existing_envelope_cleared_by_default(live):
    clip = live._song.tracks[0].clip_slots[0].clip
    live._set_clip_automation(0, 0, [{"time": 0, "value": 100.0}], "device", 0, 1)
    live._set_clip_automation(0, 0, [{"time": 0, "value": 300.0}], "device", 0, 1)
    assert env_of(clip, freq(live)).steps == [(0.0, 8.0, 300.0)]


def test_clear_existing_false_keeps_steps(live):
    clip = live._song.tracks[0].clip_slots[0].clip
    live._set_clip_automation(0, 0, [{"time": 0, "value": 100.0}], "device", 0, 1)
    live._set_clip_automation(0, 0, [{"time": 4, "value": 300.0}], "device", 0, 1,
                              clear_existing=False)
    assert len(env_of(clip, freq(live)).steps) == 2


def test_mixer_targets(live):
    track = live._song.tracks[0]
    clip = track.clip_slots[0].clip
    live._set_clip_automation(0, 0, [{"time": 0, "value": 0.3}], "volume")
    live._set_clip_automation(0, 0, [{"time": 0, "value": -0.5}], "panning")
    live._set_clip_automation(0, 0, [{"time": 0, "value": 0.7}], "send",
                              send_index=1)
    assert env_of(clip, track.mixer_device.volume).steps[0][2] == 0.3
    assert env_of(clip, track.mixer_device.panning).steps[0][2] == -0.5
    assert env_of(clip, track.mixer_device.sends[1]).steps[0][2] == 0.7


def test_arrangement_clip(live):
    live._set_clip_automation(0, 0, [{"time": 0, "value": 0.0},
                                     {"time": 16, "value": 1.0}],
                              "send", send_index=0, clip_location="arrangement",
                              resolution=1.0)
    clip = live._song.tracks[0].arrangement_clips[0]
    assert len(env_of(clip, live._song.tracks[0].mixer_device.sends[0]).steps) == 16


def test_invalid_input_writes_nothing(live):
    clip = live._song.tracks[0].clip_slots[0].clip
    live._set_clip_automation(0, 0, [{"time": 0, "value": 100.0}], "device", 0, 1)
    with pytest.raises(ValueError):
        live._set_clip_automation(0, 0, [{"time": 0, "value": 99999.0}],
                                  "device", 0, 1)
    assert env_of(clip, freq(live)).steps == [(0.0, 8.0, 100.0)]


@pytest.mark.parametrize("kwargs,exc", [
    (dict(track_index=3, clip_index=0), IndexError),
    (dict(track_index=0, clip_index=1), ValueError),          # empty slot
    (dict(track_index=0, clip_index=9), IndexError),
    (dict(track_index=0, clip_index=0, clip_location="x"), ValueError),
    (dict(track_index=0, clip_index=2, clip_location="arrangement"), IndexError),
    (dict(track_index=0, clip_index=0, parameter_type="filter"), ValueError),
    (dict(track_index=0, clip_index=0, parameter_type="device"), ValueError),
    (dict(track_index=0, clip_index=0, device_index=5, parameter_index=0), IndexError),
    (dict(track_index=0, clip_index=0, device_index=0, parameter_index=7), IndexError),
    (dict(track_index=0, clip_index=0, parameter_type="send"), ValueError),
    (dict(track_index=0, clip_index=0, parameter_type="send", send_index=4), IndexError),
])
def test_resolution_errors(live, kwargs, exc):
    with pytest.raises(exc):
        live._set_clip_automation(points=[{"time": 0, "value": 0.5}], **kwargs)


def test_clear_one_and_all(live):
    clip = live._song.tracks[0].clip_slots[0].clip
    live._set_clip_automation(0, 0, [{"time": 0, "value": 0.3}], "volume")
    live._set_clip_automation(0, 0, [{"time": 0, "value": 100.0}], "device", 0, 1)
    live._clear_clip_automation(0, 0, "volume")
    assert len(clip.envelopes) == 1
    res = live._clear_clip_automation(0, 0)
    assert clip.cleared_all and not clip.envelopes and res["cleared"] == "all"


def test_commands_route_through_process_command(live):
    live.schedule_message = lambda delay, fn: fn()
    resp = live._process_command({"type": "set_clip_automation", "params": {
        "track_index": 0, "clip_index": 0, "parameter_type": "volume",
        "points": [{"time": 0, "value": 0.1}, {"time": 4, "value": 0.9}]}})
    assert resp["status"] == "success" and resp["result"]["step_count"] == 17
    resp = live._process_command({"type": "clear_clip_automation", "params": {
        "track_index": 0, "clip_index": 0}})
    assert resp["status"] == "success"
    resp = live._process_command({"type": "set_clip_automation", "params": {
        "track_index": 0, "clip_index": 0, "parameter_type": "volume",
        "points": [{"time": 0, "value": 2.0}]}})
    assert resp["status"] == "error"


# ---------------------------------------------------------------- server side

def test_server_set_clip_automation_forwards_params(fake_conn):
    conn = fake_conn({"parameter": "Auto Filter: Frequency", "clip_name": "Loop",
                      "clip_location": "session", "step_count": 32,
                      "clip_length": 8.0,
                      "check": [{"time": 0.0, "value": 200.0}]})
    pts = [{"time": 0, "value": 200.0}, {"time": 8, "value": 12000.0}]
    out = call(server.set_clip_automation, None, 2, 0, pts,
               device_index=0, parameter_index=1)
    assert conn.sent == [("set_clip_automation", {
        "track_index": 2, "clip_index": 0, "points": pts,
        "parameter_type": "device", "device_index": 0, "parameter_index": 1,
        "send_index": None, "clip_location": "session", "resolution": 0.25,
        "clear_existing": True})]
    assert "Frequency" in out and "32 steps" in out


def test_server_clear_clip_automation(fake_conn):
    conn = fake_conn({"cleared": "all", "clip_name": "Loop",
                      "clip_location": "session"})
    out = call(server.clear_clip_automation, None, 1, 3)
    assert conn.sent[0][0] == "clear_clip_automation"
    assert conn.sent[0][1]["parameter_type"] == "all"
    assert "Cleared" in out


def test_server_error_is_tool_error(fake_conn):
    fake_conn(raise_exc=server.AbletonCommandError("value out of range"))
    with pytest.raises(server.ToolError):
        call(server.set_clip_automation, None, 0, 0, [{"time": 0, "value": 9}],
             "volume")


def test_commands_get_modifying_timeout():
    import inspect as _i
    src = _i.getsource(server.AbletonConnection._send_command_locked)
    assert '"set_clip_automation"' in src and '"clear_clip_automation"' in src
