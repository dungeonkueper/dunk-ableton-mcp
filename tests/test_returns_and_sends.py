"""Tests for create_return_track and set_send.

Live-side: the Remote Script methods against a fake Song that mimics the Live
API (Song.create_return_track() appends a return and gives every track a new
send). Server-side: the MCP tools with the socket connection mocked.
"""

import asyncio
import inspect
import math

import pytest

import MCP_Server.server as server
from tests.test_remote_script_bridge import _load_script_module, make_instance


# ---------------------------------------------------------------- Live fakes

class FakeParam(object):
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0, enabled=True):
        self.name = name
        self.value = value
        self.min = lo
        self.max = hi
        self.is_enabled = enabled


class FakeMixer(object):
    def __init__(self):
        self.sends = []


class FakeTrack(object):
    def __init__(self, name):
        self.name = name
        self.mixer_device = FakeMixer()


class FakeSong(object):
    def __init__(self, n_tracks=2):
        self.tracks = [FakeTrack("%d-Audio" % (i + 1)) for i in range(n_tracks)]
        self.return_tracks = []

    def create_return_track(self):
        letter = chr(ord("A") + len(self.return_tracks))
        self.return_tracks.append(FakeTrack("%s-Return" % letter))
        for t in self.tracks + self.return_tracks:
            while len(t.mixer_device.sends) < len(self.return_tracks):
                t.mixer_device.sends.append(
                    FakeParam(chr(ord("A") + len(t.mixer_device.sends))))


@pytest.fixture(scope="module")
def rs():
    return _load_script_module()


@pytest.fixture
def live(rs):
    inst = make_instance(rs)
    inst._song = FakeSong()
    return inst


def test_capabilities_advertised(rs):
    assert "create_return_track" in rs.SCRIPT_CAPABILITIES
    assert "set_send" in rs.SCRIPT_CAPABILITIES


def test_create_return_track_appends_and_reports_send_index(live):
    first = live._create_return_track()
    second = live._create_return_track()
    assert first == {"index": 0, "name": "A-Return", "send_index": 0,
                     "return_track_count": 1}
    assert second["index"] == second["send_index"] == 1
    assert len(live._song.tracks[0].mixer_device.sends) == 2


def test_set_send_sets_value_and_reports_old(live):
    live._create_return_track()
    res = live._set_send(1, 0, 0.5)
    assert live._song.tracks[1].mixer_device.sends[0].value == 0.5
    assert res["old_value"] == 0.0 and res["value"] == 0.5
    assert res["return_track_name"] == "A-Return"
    assert res["track_name"] == "2-Audio"


def test_set_send_on_return_track(live):
    live._create_return_track()
    live._create_return_track()
    live._set_send(0, 1, 0.3, track_type="return")
    assert live._song.return_tracks[0].mixer_device.sends[1].value == 0.3


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"),
                                   "0.5", True, None])
def test_set_send_rejects_bad_values_without_writing(live, value):
    live._create_return_track()
    with pytest.raises(ValueError):
        live._set_send(0, 0, value)
    assert live._song.tracks[0].mixer_device.sends[0].value == 0.0


@pytest.mark.parametrize("track_index,send_index", [(5, 0), (-1, 0), (0, 1), (0, -1)])
def test_set_send_index_errors(live, track_index, send_index):
    live._create_return_track()
    with pytest.raises(IndexError):
        live._set_send(track_index, send_index, 0.5)


def test_set_send_without_returns_fails(live):
    with pytest.raises(IndexError):
        live._set_send(0, 0, 0.5)


def test_set_send_rejects_unknown_track_type(live):
    live._create_return_track()
    with pytest.raises(ValueError):
        live._set_send(0, 0, 0.5, track_type="master")


def test_set_send_disabled_send(live):
    live._create_return_track()
    live._song.tracks[0].mixer_device.sends[0].is_enabled = False
    with pytest.raises(RuntimeError):
        live._set_send(0, 0, 0.5)


def test_commands_route_through_command_dispatch(live):
    resp = live._execute_command({"type": "create_return_track", "params": {}})
    assert resp["status"] == "success" and resp["result"]["index"] == 0
    resp = live._execute_command({"type": "set_send", "params": {
        "track_index": 0, "send_index": 0, "value": 0.7}})
    assert resp["status"] == "success"
    assert math.isclose(live._song.tracks[0].mixer_device.sends[0].value, 0.7)
    resp = live._execute_command({"type": "set_send", "params": {
        "track_index": 0, "send_index": 3, "value": 0.7}})
    assert resp["status"] == "error"


# ---------------------------------------------------------------- server side

def call(tool, *args, **kwargs):
    result = tool(*args, **kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


class FakeConnection(object):
    def __init__(self, response=None, raise_exc=None):
        self.response = response or {}
        self.raise_exc = raise_exc
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    class _Dummy(object):
        def record_event(self, *a, **k):
            pass
    import MCP_Server.telemetry_decorator as td
    import MCP_Server.dataset.consent as consent
    import MCP_Server.script_handshake as sh
    monkeypatch.setattr(td, "get_telemetry", lambda: _Dummy(), raising=False)
    monkeypatch.setattr(consent, "maybe_consent_notice", lambda: "")
    monkeypatch.setattr(sh, "require_capability", lambda name: None)


@pytest.fixture
def fake_conn(monkeypatch):
    def _make(response=None, raise_exc=None):
        conn = FakeConnection(response, raise_exc)
        monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
        return conn
    return _make


def test_server_create_return_track(fake_conn):
    conn = fake_conn({"index": 2, "name": "C-Return", "send_index": 2})
    out = call(server.create_return_track, None)
    assert conn.sent == [("create_return_track", {})]
    assert "C-Return" in out and "send_index 2" in out


def test_server_set_send_forwards_params(fake_conn):
    conn = fake_conn({"send_name": "A", "track_name": "Drums",
                      "return_track_name": "A-Reverb",
                      "old_value": 0.0, "value": 0.5})
    out = call(server.set_send, None, 1, 0, 0.5)
    assert conn.sent == [("set_send", {"track_index": 1, "send_index": 0,
                                       "value": 0.5, "track_type": "track"})]
    assert "A-Reverb" in out and "0.5" in out


def test_server_set_send_error_is_tool_error(fake_conn):
    fake_conn(raise_exc=server.AbletonCommandError("Send index 4 out of range"))
    with pytest.raises(server.ToolError) as exc:
        call(server.set_send, None, 0, 4, 0.5)
    assert "out of range" in str(exc.value)


def test_both_commands_get_modifying_timeout():
    import inspect as _i
    src = _i.getsource(server.AbletonConnection._send_command_locked)
    assert '"create_return_track"' in src and '"set_send"' in src
