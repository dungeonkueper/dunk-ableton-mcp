# Device registry (prototype)

Set device parameters by name and in real units, without listing the device first.

```text
set_device_parameters_by_name(track_index=0, changes={
    "cutoff": "800 Hz", "resonance": 0.4, "Osc 1 Wave": "Saw",
    "attack": "12 ms", "Volume": "-6 dB", "Dry/Wet": {"normalized": 0.3}})
```

## No Ableton data in this repository

The repository ships only the scanner, the converter and the format below.
The registry is built on the user's machine: the first time the server touches
a device it sends a read-only `scan_device` to the Remote Script, which reads
names, ranges, step items and a few `str_for_value()` samples from the user's own
Live. The result is cached as JSON:

```
~/.cache/ableton-mcp/device-registry/<live version>/<ClassName>-<fingerprint>.json
```

(`$XDG_CACHE_HOME` and `ABLETON_MCP_REGISTRY_DIR` override the location.) The
fingerprint is a hash of the class name and the parameter names, so plug-ins
(which share a class name), racks with renamed macros and new Live versions
each get their own entry. Typical entries are a few KB to about 20 KB.

## Roundtrips

| Situation | Calls to Live |
|---|---|
| Device never seen | light scan + full scan + write |
| Device seen before (cache on disk, new server process) | light scan + write |
| Same server process, same device | write only |
| Device replaced or script reloaded (`stale_target`) | write fails, light scan, write once more |

The write is one guarded command (`set_device_parameters`): track, device and
parameter identities from the scan, `expected_class` and each parameter's
expected name are checked, every change is preflighted, then all are applied,
verified and rolled back together. Upstream wraps each command in one undo step.

## Values

| Input | Meaning |
|---|---|
| `0.42` | raw Live parameter value (as `set_device_parameter`) |
| `"800 Hz"`, `"1.2 kHz"`, `"250 ms"`, `"-6 dB"`, `"40 %"` | real units; Live converts exactly via `display_value` |
| `"+7 st"`, `"30L"`, `"-inf dB"` | converted from the cached curve; flagged `approximate` |
| `"Saw"`, `"saw"`, `"Sq"` | item of a stepped parameter (case-insensitive, unique prefix) |
| `{"normalized": 0.3}` | position in the raw range |
| `true` / `false` | two-step switches |

Wrong units, out-of-range values and unknown items are rejected before
anything is sent; errors list the valid range or items. `dry_run=true` shows
the plan only.

## Names

Exact name, then case/space-insensitive name, then user aliases, then built-in
generic aliases (`cutoff`, `brightness`, `resonance`, `attack`, `mix`, ...),
then a unique partial match. `"#12"` addresses by index. Ambiguous names fail
and list candidates. User aliases live in `<registry dir>/aliases.json`:

```json
{"*": {"brightness": "Filter Freq"}, "Operator": {"bite": "Filter Res"}}
```

## MCP surface

* tool `scan_device` — learn/refresh a device and return its compact schema
* tool `set_device_parameters_by_name` — the batch write above
* resource `ableton://devices` — learned devices (cache only, no Live call)
* resource `ableton://devices/{key}` — compact schema of one device

Remote Script commands: `scan_device` (read-only), `set_device_parameters`
(guarded, safety category `edit`). Older scripts lack the capability and the
tools say so.

## Entry format (schema_version 1)

```json
{
  "schema": "ableton-mcp/device-registry", "schema_version": 1,
  "key": "FakeSynth-1a2b3c4d5e6f", "class_name": "FakeSynth",
  "class_display_name": "Fake Synth", "device_name": "My Synth",
  "live_version": "12.4.5", "scanned_at": "2026-11-28T13:40:00Z",
  "parameter_count": 7, "parameters_truncated": false,
  "parameters": [
    {"index": 1, "name": "Filter Freq", "original_name": "Filter Freq",
     "min": 0.0, "max": 1.0, "quantized": false, "unit": "Hz",
     "display_min": 20.0, "display_max": 20000.0, "monotonic": true,
     "curve": [[0.0, 20.0], [0.03125, 24.9], "..."]},
    {"index": 3, "name": "Osc 1 Wave", "min": 0.0, "max": 2.0,
     "quantized": true, "items": ["Sine", "Saw", "Square"]}
  ]
}
```

Units are canonical: `Hz`, `s`, `dB`, `%`, `st`, `ct`, `pan`, `""` (plain
number) or `null` (display not numeric, e.g. sync rates). Cache files may
contain `-Infinity` for `-inf dB`; resources and tool output use the string
`"-inf"`.

## Not done yet

* Not tested against a real Live. `default_value`, `original_name` and
  `class_display_name` are read defensively.
* Compact session snapshot with change counter (idea 4) and macro profiles
  ("warmer bass") are left for later.
