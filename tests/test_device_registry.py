"""Device registry prototype: Remote Script scan/batch + server-side registry.

Everything runs against fakes; no Live, no Ableton device data.
"""
import json
import math
import types

import pytest

from MCP_Server.device_registry import (
    DeviceRegistry, RegistryClient, RegistryError, build_entry, compact_entry, infer_unit,
    parse_display, plan_change, registry_key, resolve_parameter,
)
from MCP_Server.server import AbletonCommandError
from tests.test_remote_script_bridge import _load_script_module, make_instance


class FakeParam(object):
    def __init__(self, name, value, low=0.0, high=1.0, fmt=None, items=None):
        self.name = self.original_name = name
        self.value, self.min, self.max = value, low, high
        self.is_enabled = True
        self.is_quantized = items is not None
        self.value_items = items or []
        self._fmt = fmt or (lambda v: "%.2f" % v)
        self.formatted = 0

    def str_for_value(self, value):
        self.formatted += 1
        if self.is_quantized:
            return self.value_items[int(round(value - self.min))]
        return self._fmt(value)


def hz(v):
    f = 20.0 * 1000.0 ** v  # 20 Hz .. 20 kHz, exponential like a filter knob
    return "%.2f kHz" % (f / 1000.0) if f >= 1000 else "%.1f Hz" % f


def db(v):
    return "-inf dB" if v == 0 else "%.1f dB" % (-60 + 66 * v)


def make_device():
    return types.SimpleNamespace(
        name="My Synth", class_name="FakeSynth", class_display_name="Fake Synth", can_have_chains=False,
        parameters=[
            FakeParam("Device On", 1, 0, 1, items=["Off", "On"]),
            FakeParam("Filter Freq", 0.5, fmt=hz),
            FakeParam("Filter Res", 0.2),
            FakeParam("Osc 1 Wave", 0, 0, 2, items=["Sine", "Saw", "Square"]),
            FakeParam("Amp Attack", 0.1, fmt=lambda v: "%.1f ms" % (v * 2000)),
            FakeParam("Volume", 0.5, fmt=db),
            FakeParam("Transpose", 0, -48, 48, fmt=lambda v: "%+d st" % round(v)),
        ])


@pytest.fixture
def live(monkeypatch, tmp_path):
    (tmp_path / "safety.json").write_text("{}")
    monkeypatch.setenv("ABLETON_MCP_SAFETY_CONFIG", str(tmp_path / "safety.json"))
    rs = _load_script_module()
    app = types.SimpleNamespace(get_major_version=lambda: 12, get_minor_version=lambda: 4,
                                get_bugfix_version=lambda: 5)
    inst = make_instance(rs, application=app)
    device = make_device()
    track = types.SimpleNamespace(name="Bass", devices=[device])
    inst._song = types.SimpleNamespace(tracks=[track], return_tracks=[], master_track=track, file_path="")
    calls = []

    def send(command, params):
        calls.append(command)
        response = inst._execute_command({"type": command, "params": json.loads(json.dumps(params))})
        if isinstance(response, rs.DeferredOperation):
            try:
                result = response.poll()
            except Exception as error:
                response.close(True)
                raise AbletonCommandError(str(error), {"code": getattr(error, "code", "execution_failed")})
            response.close()
            return result
        if response["status"] == "error":
            raise AbletonCommandError(response["message"], response["error"])
        return response["result"]

    return types.SimpleNamespace(rs=rs, inst=inst, device=device, track=track, send=send, calls=calls,
                                 registry=DeviceRegistry(tmp_path / "registry"))


def values(device):
    return [p.value for p in device.parameters]


# ----------------------------------------------------------- remote script ---

def test_scan_is_read_only_and_reports_identities_units_and_items(live):
    before = values(live.device)
    scan = live.send("scan_device", {"track_index": 0, "sample_points": 5})
    assert values(live.device) == before
    assert scan["class_name"] == "FakeSynth" and scan["live_version"] == "12.4.5"
    assert scan["target_id"] and scan["device_id"]
    freq = scan["parameters"][1]
    assert freq["samples"][0] == [0.0, "20.0 Hz"] and freq["samples"][-1] == [1.0, "20.00 kHz"]
    assert scan["parameters"][3]["value_items"] == ["Sine", "Saw", "Square"]
    assert "samples" not in scan["parameters"][3]


def test_light_scan_has_no_samples(live):
    scan = live.send("scan_device", {"track_index": 0})
    assert all("samples" not in p for p in scan["parameters"])
    with pytest.raises(AbletonCommandError):
        live.send("scan_device", {"track_index": 0, "sample_points": 99})


def test_batch_preflights_everything_before_writing(live):
    scan = live.send("scan_device", {"track_index": 0})
    base = {"track_index": 0, "target_id": scan["target_id"], "device_id": scan["device_id"]}
    good = {"parameter_index": 2, "expected_name": "Filter Res", "value": 0.9}
    bad = {"parameter_index": 3, "expected_name": "Osc 1 Wave", "value": 1.5}
    before = values(live.device)
    with pytest.raises(AbletonCommandError, match="integer"):
        live.send("set_device_parameters", dict(base, changes=[good, bad]))
    assert values(live.device) == before
    renamed = dict(good, expected_name="Old Name")
    with pytest.raises(AbletonCommandError) as info:
        live.send("set_device_parameters", dict(base, changes=[renamed]))
    assert info.value.error["code"] == "stale_target"
    result = live.send("set_device_parameters", dict(base, changes=[
        good, {"parameter_index": 1, "expected_name": "Filter Freq", "display_value": "1 kHz"}]))
    assert live.device.parameters[2].value == 0.9
    assert parse_display(result["changed"][1]["display"])[0] == pytest.approx(1000, rel=1e-3)


def test_batch_requires_identities(live):
    with pytest.raises(AbletonCommandError) as info:
        live.send("set_device_parameters", {"track_index": 0, "changes": [
            {"parameter_index": 2, "expected_name": "Filter Res", "value": 0.3}]})
    assert info.value.error["code"] == "identity_required"


def test_new_commands_are_advertised(live):
    caps = live.inst._get_script_info()["capabilities"]
    assert "scan_device" in caps and "set_device_parameters" in caps


# ------------------------------------------------------------------- units ---

@pytest.mark.parametrize("text,expected", [
    ("1.00 kHz", (1000.0, "Hz")), ("250 ms", (0.25, "s")), ("-6.0 dB", (-6.0, "dB")),
    ("-inf dB", (-math.inf, "dB")), ("40 %", (40.0, "%")), ("+7 st", (7.0, "st")),
    ("30L", (-30.0, "pan")), ("C", (0.0, "pan")), ("0.40", (0.4, "")),
])
def test_parse_display(text, expected):
    assert parse_display(text) == expected


@pytest.mark.parametrize("text", ["Saw", "1/16", "Off", "inf Hz", ""])
def test_parse_display_rejects_non_numbers(text):
    assert parse_display(text) is None


def test_infer_unit_requires_one_unit_and_variation():
    assert infer_unit([[0, "1 Hz"], [1, "1 kHz"]])[0] == "Hz"
    assert infer_unit([[0, "1 Hz"], [1, "1 dB"]]) == (None, [])
    assert infer_unit([[0, "0.00"], [1, "0.00"]]) == (None, [])


# -------------------------------------------------------- registry entries ---

@pytest.fixture
def entry(live):
    return build_entry(live.send("scan_device", {"track_index": 0, "sample_points": 33}))


def test_entry_is_keyed_by_class_and_parameter_names(entry):
    names = [p["name"] for p in entry["parameters"]]
    assert entry["key"] == registry_key("FakeSynth", names)
    assert entry["key"] != registry_key("FakeSynth", names[:-1])
    freq = entry["parameters"][1]
    assert freq["unit"] == "Hz" and freq["monotonic"]
    assert freq["display_min"] == pytest.approx(20) and freq["display_max"] == pytest.approx(20000)
    assert entry["parameters"][2]["unit"] == ""
    assert "parameter_id" not in json.dumps(entry)  # no live identities on disk


def test_compact_entry_is_strict_json(entry):
    text = json.dumps(compact_entry(entry), allow_nan=False)
    assert '"-inf"' in text  # Volume's -inf dB end


@pytest.mark.parametrize("query,index", [
    ("Filter Freq", 1), ("filter freq", 1), ("cutoff", 1), ("brightness", 1), ("resonance", 2),
    ("attack", 4), ("wave", 3), ("#6", 6), (5, 5),
])
def test_resolve_parameter(entry, query, index):
    assert resolve_parameter(entry, query)["index"] == index


def test_resolve_parameter_errors_suggest(entry):
    with pytest.raises(RegistryError, match="Filter Freq"):
        resolve_parameter(entry, "Filter Frq")
    with pytest.raises(RegistryError, match="ambiguous"):
        resolve_parameter(entry, "filter")


def test_user_aliases(entry):
    aliases = {"FakeSynth": {"bite": "Filter Res"}, "*": {"loudness": "Volume"}}
    assert resolve_parameter(entry, "bite", aliases)["index"] == 2
    assert resolve_parameter(entry, "loudness", aliases)["index"] == 5


def test_plan_change_units(entry):
    p = entry["parameters"]
    freq = plan_change(p[1], "800 Hz")
    assert freq["display_value"] == "800.0 Hz" and not freq["approximate"]
    assert freq["estimated_value"] == pytest.approx(math.log(40) / math.log(1000), abs=0.01)
    assert plan_change(p[1], "1.2 kHz")["display_value"] == "1200.0 Hz"
    assert plan_change(p[4], "12 ms")["display_value"] == "12.0 ms"
    assert plan_change(p[3], "saw")["value"] == 1
    assert plan_change(p[3], "Sq")["value"] == 2
    assert plan_change(p[0], False)["value"] == 0
    assert plan_change(p[2], {"normalized": 0.25})["value"] == 0.25
    vol = plan_change(p[5], "-inf dB")
    assert vol["value"] == 0 and vol["approximate"]
    st = plan_change(p[6], "+7 st")
    assert st["approximate"] and st["value"] == pytest.approx(7, abs=0.01)


@pytest.mark.parametrize("index,value,message", [
    (1, "800", "needs a unit"), (1, "-6 dB", "measured in Hz"), (1, "30 kHz", "outside"),
    (3, "Triangle", "not one of"), (3, 1.5, "whole steps"), (2, 3.0, "outside"), (2, "loud", "cannot read"),
    (2, {"normalized": 2}, "normalized"),
])
def test_plan_change_rejections(entry, index, value, message):
    with pytest.raises(RegistryError, match=message):
        plan_change(entry["parameters"][index], value)


# ------------------------------------------------------------- end to end ---

def test_first_contact_scans_and_caches_then_writes_by_name(live):
    client = RegistryClient(live.send, live.registry)
    result = client.set_parameters("track", 0, [0], {"cutoff": "1 kHz", "Osc 1 Wave": "Saw", "attack": "200 ms"})
    assert result["scans"] == ["light", "full"]
    p = live.device.parameters
    assert parse_display(hz(p[1].value))[0] == pytest.approx(1000, rel=1e-3) and p[3].value == 1 and p[4].value == pytest.approx(0.1, abs=1e-3)
    assert live.calls == ["scan_device", "scan_device", "set_device_parameters"]
    files = list((live.registry.directory / "12.4.5").glob("FakeSynth-*.json"))
    assert len(files) == 1

    live.calls.clear()
    client.set_parameters("track", 0, [0], {"resonance": 0.7})
    assert live.calls == ["set_device_parameters"]  # warm: one roundtrip

    # New server process: cache on disk, identities gone -> light scan only.
    live.calls.clear()
    fresh = RegistryClient(live.send, DeviceRegistry(live.registry.directory))
    assert fresh.set_parameters("track", 0, [0], {"Volume": "0 dB"})["scans"] == ["light"]
    assert live.calls == ["scan_device", "set_device_parameters"]


def test_validation_failure_sends_no_write(live):
    client = RegistryClient(live.send, live.registry)
    before = values(live.device)
    with pytest.raises(RegistryError) as info:
        client.set_parameters("track", 0, [0], {"cutoff": "50 kHz", "nope": 1})
    assert "outside" in str(info.value) and "nope" in str(info.value)
    assert "set_device_parameters" not in live.calls
    assert values(live.device) == before


def test_dry_run_does_not_write(live):
    client = RegistryClient(live.send, live.registry)
    before = values(live.device)
    plan = client.set_parameters("track", 0, [0], {"cutoff": "800 Hz"}, dry_run=True)
    assert plan["dry_run"] and plan["plan"][0]["send"] == "800.0 Hz"
    assert values(live.device) == before


def test_stale_identity_rescans_once(live):
    client = RegistryClient(live.send, live.registry)
    client.set_parameters("track", 0, [0], {"resonance": 0.3})
    # Device replaced by an identical one (e.g. undo/redo, script reload).
    live.track.devices[0] = make_device()
    live.calls.clear()
    result = client.set_parameters("track", 0, [0], {"resonance": 0.6})
    assert result["retried_after"] == "stale_target"
    assert live.calls == ["set_device_parameters", "scan_device", "set_device_parameters"]
    assert live.track.devices[0].parameters[2].value == 0.6


def test_different_device_at_same_slot_is_not_written_with_old_plan(live):
    client = RegistryClient(live.send, live.registry)
    client.set_parameters("track", 0, [0], {"resonance": 0.3})
    other = make_device(); other.class_name = "OtherDevice"; other.parameters[2].name = "Drive"
    live.track.devices[0] = other
    with pytest.raises(RegistryError, match="resonance"):
        client.set_parameters("track", 0, [0], {"resonance": 0.6})
    assert other.parameters[2].value == 0.2


def test_corrupt_cache_file_is_ignored(live):
    client = RegistryClient(live.send, live.registry)
    entry, _, _ = client.ensure("track", 0, [0])
    path = live.registry.directory / "12.4.5" / (entry["key"] + ".json")
    path.write_text("{broken")
    fresh = RegistryClient(live.send, DeviceRegistry(live.registry.directory))
    assert fresh.ensure("track", 0, [0])[2]["scans"] == ["light", "full"]
