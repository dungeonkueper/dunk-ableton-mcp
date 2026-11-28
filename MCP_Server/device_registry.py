"""Local device registry: learn a device's parameters once, then set them by name.

Nothing about Ableton's devices ships with this package. The first time the
server touches a device it asks the Remote Script for a read-only scan
(``scan_device``) of that device in the user's own Live, builds a registry
entry from it and caches it as JSON under the user's cache directory. The
entry is keyed by device class, a fingerprint of the parameter names, and the
Live version, so it also covers plug-ins and new Live releases.

An entry knows, per parameter: index, name, range, whether it is quantized
(with its items), the display unit inferred from sampled ``str_for_value``
output and a coarse value→display curve. With it, the server can

* resolve "cutoff", "Filter Freq" or "#12" to a parameter index,
* turn "800 Hz", "-6 dB", "250 ms", "Saw" or {"normalized": 0.3} into a
  write Live understands, and validate it before anything is sent,
* write several parameters of one device in one guarded command.

Exact unit conversion stays in Live where it can (``display_value`` is
resolved by the Remote Script against Live's own formatter). The cached curve
is used for validation, dry runs, and units Live's parser does not accept
(semitones, pan, displays with -inf); those results are flagged approximate.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_ID = "ableton-mcp/device-registry"
SCHEMA_VERSION = 1
DEFAULT_SAMPLE_POINTS = 33

# Units the Remote Script's display parser accepts (_parse_display).
LIVE_PARSEABLE_UNITS = {"Hz", "s", "dB", "%", ""}

# Generic synth/effect vocabulary → parameter-name candidates (normalized).
# These are guesses matched against the names found by a scan, not device data.
GENERIC_ALIASES: dict[str, list[str]] = {
    "cutoff": ["filterfreq", "filter1freq", "filterfrequency", "frequency", "freq", "cutoff", "filtercutoff", "fe1freq", "filter1cutoff"],
    "brightness": ["filterfreq", "filter1freq", "frequency", "cutoff", "tone", "brightness", "hicut"],
    "resonance": ["filterres", "filter1res", "resonance", "res", "q", "filterq", "filterresonance"],
    "attack": ["attack", "ampattack", "aattack", "envattack", "ve attack", "amp1attack", "env1attack"],
    "decay": ["decay", "ampdecay", "adecay", "envdecay", "vedecay", "amp1decay", "env1decay"],
    "sustain": ["sustain", "ampsustain", "asustain", "envsustain", "vesustain", "amp1sustain", "env1sustain"],
    "release": ["release", "amprelease", "arelease", "envrelease", "verelease", "amp1release", "env1release"],
    "volume": ["volume", "gain", "outputgain", "output", "level", "mastervolume"],
    "gain": ["gain", "outputgain", "drive", "volume"],
    "drive": ["drive", "predrive", "dryinput", "inputgain", "amount"],
    "mix": ["drywet", "mix", "wet", "drywetmix"],
    "drywet": ["drywet", "mix", "wet"],
    "width": ["width", "stereowidth", "stereo"],
    "pan": ["pan", "panorama", "balance"],
    "feedback": ["feedback", "fb"],
    "time": ["time", "delaytime", "decaytime", "beatdelaytimel"],
    "pitch": ["transpose", "pitch", "coarse", "semitone"],
    "detune": ["detune", "fine", "spread"],
}

_UNIT_RE = re.compile(
    r"^\s*([-+]?(?:inf|\d+(?:[.,]\d+)?|[.,]\d+))\s*(khz|hz|ms|s|db|%|st|ct|cents?|semitones?|)\s*$",
    re.IGNORECASE,
)
_PAN_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([LR])\s*$|^\s*C\s*$", re.IGNORECASE)


class RegistryError(ValueError):
    """A request the registry can reject before anything is sent to Live."""


# ---------------------------------------------------------------- units ----

def parse_display(text: Any) -> tuple[float, str] | None:
    """Parse a Live display string into (number, canonical unit).

    Canonical units: Hz, s, dB, %, st, ct, pan and "" (unitless number).
    Returns None for text that is not a number with a known unit
    (e.g. "Saw", "1/16", "Off").
    """
    if not isinstance(text, str):
        return None
    pan = _PAN_RE.match(text)
    if pan:
        if pan.group(1) is None:
            return 0.0, "pan"
        number = float(pan.group(1))
        return (-number if pan.group(2).upper() == "L" else number), "pan"
    match = _UNIT_RE.match(text)
    if not match:
        return None
    raw, unit = match.group(1).replace(",", "."), match.group(2).lower()
    number = float(raw)
    if math.isinf(number) and unit != "db":
        return None
    if unit == "khz":
        return number * 1000.0, "Hz"
    if unit == "ms":
        return number / 1000.0, "s"
    canonical = {"hz": "Hz", "s": "s", "db": "dB", "%": "%", "st": "st", "semitone": "st",
                 "semitones": "st", "ct": "ct", "cent": "ct", "cents": "ct", "": ""}[unit]
    return number, canonical


def infer_unit(samples: list[list[Any]]) -> tuple[str | None, list[list[float]]]:
    """Infer one unit from (value, display) samples and return the parsed curve.

    Returns (None, []) when the displays do not share a unit or do not parse.
    Unitless displays are only accepted if they differ (a constant "0.00" is
    no evidence of a numeric scale).
    """
    units, curve = set(), []
    for value, display in samples:
        parsed = parse_display(display)
        if parsed is None:
            return None, []
        number, unit = parsed
        units.add(unit)
        curve.append([float(value), number])
    if len(units) != 1 or len(curve) < 2:
        return None, []
    unit = units.pop()
    finite = [n for _, n in curve if math.isfinite(n)]
    if len(set(finite)) < 2:
        return None, []
    return unit, curve


def _monotonic(curve: list[list[float]]) -> int:
    """+1 increasing, -1 decreasing, 0 neither (ignoring -inf endpoints)."""
    numbers = [n for _, n in curve if math.isfinite(n)]
    if len(numbers) < 2:
        return 0
    up = all(b >= a for a, b in zip(numbers, numbers[1:]))
    down = all(b <= a for a, b in zip(numbers, numbers[1:]))
    if up and numbers[-1] > numbers[0]:
        return 1
    if down and numbers[-1] < numbers[0]:
        return -1
    return 0


def curve_to_raw(curve: list[list[float]], unit: str, target: float) -> float:
    """Approximate raw value for a display number by interpolating the curve.

    Frequencies interpolate logarithmically, everything else linearly.
    """
    if math.isinf(target):
        for raw, number in curve:
            if number == target:
                return raw
        raise RegistryError("This parameter has no %s position" % ("-inf dB" if target < 0 else "inf"))
    points = [(r, n) for r, n in curve if math.isfinite(n)]
    for (r0, n0), (r1, n1) in zip(points, points[1:]):
        lo, hi = min(n0, n1), max(n0, n1)
        if lo <= target <= hi:
            if n1 == n0:
                return r0
            if unit == "Hz" and n0 > 0 and n1 > 0 and target > 0:
                t = (math.log(target) - math.log(n0)) / (math.log(n1) - math.log(n0))
            else:
                t = (target - n0) / (n1 - n0)
            return r0 + t * (r1 - r0)
    raise RegistryError("value outside the curve")


def format_for_live(number: float, unit: str) -> str:
    if unit == "s":
        return "%r ms" % (number * 1000.0)
    if unit == "":
        return repr(number)
    return "%r %s" % (number, unit)


def normalize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# --------------------------------------------------------------- entries ---

def fingerprint(class_name: str, names: list[str]) -> str:
    digest = hashlib.sha1("\0".join([class_name] + list(names)).encode("utf-8")).hexdigest()
    return digest[:12]


def registry_key(class_name: str, names: list[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", class_name) or "device"
    return "%s-%s" % (safe, fingerprint(class_name, names))


def build_entry(scan: dict[str, Any]) -> dict[str, Any]:
    """Turn a scan_device result into a registry entry (no live identities)."""
    names = [p["name"] for p in scan["parameters"]]
    parameters = []
    for p in scan["parameters"]:
        rec: dict[str, Any] = {
            "index": p["index"], "name": p["name"], "original_name": p.get("original_name", p["name"]),
            "min": p["min"], "max": p["max"], "quantized": bool(p["is_quantized"]),
        }
        if "default" in p:
            rec["default"] = p["default"]
        if rec["quantized"]:
            rec["items"] = list(p.get("value_items") or [])
        elif p.get("samples"):
            unit, curve = infer_unit(p["samples"])
            rec["unit"] = unit
            if unit is not None:
                direction = _monotonic(curve)
                rec["curve"] = curve
                rec["monotonic"] = direction != 0
                finite = [n for _, n in curve if math.isfinite(n)]
                rec["display_min"] = min(n for _, n in curve)
                rec["display_max"] = max(n for _, n in curve)
                if not finite:
                    rec["monotonic"] = False
        parameters.append(rec)
    return {
        "schema": SCHEMA_ID, "schema_version": SCHEMA_VERSION,
        "key": registry_key(scan["class_name"], names),
        "class_name": scan["class_name"],
        "class_display_name": scan.get("class_display_name", scan["class_name"]),
        "device_name": scan.get("name", ""),
        "live_version": scan.get("live_version"),
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parameter_count": scan.get("parameter_count", len(parameters)),
        "parameters_truncated": bool(scan.get("parameters_truncated")),
        "parameters": parameters,
    }


def validate_entry(entry: Any) -> None:
    """Minimal structural check for cached files (they may be hand-edited)."""
    if not isinstance(entry, dict) or entry.get("schema") != SCHEMA_ID:
        raise RegistryError("not a device registry entry")
    if entry.get("schema_version") != SCHEMA_VERSION:
        raise RegistryError("unsupported registry schema_version")
    for key in ("key", "class_name", "parameters"):
        if key not in entry:
            raise RegistryError("registry entry lacks " + key)
    for i, p in enumerate(entry["parameters"]):
        if not isinstance(p, dict) or p.get("index") != i or not isinstance(p.get("name"), str):
            raise RegistryError("registry parameter %d is malformed" % i)
        if not all(isinstance(p.get(k), (int, float)) for k in ("min", "max")):
            raise RegistryError("registry parameter %d lacks a numeric range" % i)


def jsonable(value: Any) -> Any:
    """Replace non-finite floats (e.g. -inf dB) with strings for strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if value != value else ("inf" if value > 0 else "-inf")
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Token-lean view for Claude: no curves, one line of facts per parameter."""
    params = []
    for p in entry["parameters"]:
        row: dict[str, Any] = {"i": p["index"], "name": p["name"]}
        if p["quantized"]:
            row["items"] = p.get("items", [])
        elif p.get("unit") is not None:
            row["unit"] = p["unit"]
            row["range"] = [p["display_min"], p["display_max"]]
        else:
            row["range"] = [p["min"], p["max"]]
        params.append(row)
    return jsonable({"key": entry["key"], "class_name": entry["class_name"], "device_name": entry.get("device_name"),
                     "live_version": entry.get("live_version"), "parameters": params})


# ------------------------------------------------------------ resolution ---

def resolve_parameter(entry: dict[str, Any], query: Any, user_aliases: dict[str, Any] | None = None) -> dict[str, Any]:
    """Find one parameter by index, "#index", exact/normalized name or alias."""
    params = entry["parameters"]
    if isinstance(query, int) and not isinstance(query, bool):
        if 0 <= query < len(params):
            return params[query]
        raise RegistryError("parameter index %d out of range" % query)
    if not isinstance(query, str) or not query.strip():
        raise RegistryError("parameter must be a name or an index")
    text = query.strip()
    if re.fullmatch(r"#\d+", text):
        return resolve_parameter(entry, int(text[1:]))

    def unique(matches: list[dict[str, Any]], how: str) -> dict[str, Any] | None:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RegistryError("%r is ambiguous (%s): %s; use '#index'" % (
                query, how, ", ".join("#%d %s" % (p["index"], p["name"]) for p in matches[:8])))
        return None

    found = unique([p for p in params if p["name"] == text], "exact name")
    if found:
        return found
    wanted = normalize_name(text)
    found = unique([p for p in params if normalize_name(p["name"]) == wanted
                    or normalize_name(p.get("original_name", "")) == wanted], "name")
    if found:
        return found
    aliases = user_aliases or {}
    for scope in (entry["class_name"], "*"):
        target = (aliases.get(scope) or {}).get(text) or (aliases.get(scope) or {}).get(wanted)
        if target is not None and target != query:
            return resolve_parameter(entry, target)
    for candidate in GENERIC_ALIASES.get(wanted, []):
        cand = normalize_name(candidate)
        found = unique([p for p in params if normalize_name(p["name"]) == cand], "alias " + wanted)
        if found:
            return found
    if len(wanted) >= 3:
        found = unique([p for p in params if wanted in normalize_name(p["name"])], "partial name")
        if found:
            return found
    close = difflib.get_close_matches(text, [p["name"] for p in params], n=6, cutoff=0.5)
    hint = (" Did you mean: " + ", ".join(close) + "?") if close else ""
    raise RegistryError("No parameter matches %r on %s.%s" % (query, entry["class_name"], hint))


def plan_change(param: dict[str, Any], wanted: Any) -> dict[str, Any]:
    """Translate a user value into a Remote Script change, without sending it.

    Accepted values:
    * number: raw Live parameter value (as today),
    * {"normalized": 0..1}: position within the parameter's range,
    * {"raw": number}: explicit raw value,
    * string for quantized parameters: an item name ("Saw", case-insensitive,
      unique prefix allowed),
    * string with a unit for continuous ones: "800 Hz", "1.2 kHz", "-6 dB",
      "250 ms", "40 %", "+7 st", "30L".
    * bool: off/on for two-step quantized parameters.
    Returns {"parameter_index", "expected_name", "value"|"display_value",
    "estimated_value", "approximate"}.
    """
    out: dict[str, Any] = {"parameter_index": param["index"], "expected_name": param["name"]}
    low, high = float(param["min"]), float(param["max"])

    def raw(value: float, approximate: bool = False) -> dict[str, Any]:
        if not (math.isfinite(value) and low - 1e-9 <= value <= high + 1e-9):
            raise RegistryError("%s: raw value %r outside %r..%r" % (param["name"], value, low, high))
        value = min(max(value, low), high)
        if param["quantized"]:
            if abs(value - round(value)) > 1e-9 and not approximate:
                raise RegistryError("%s takes whole steps (%s)" % (param["name"], _items_hint(param)))
            value = float(round(value))
        out.update(value=value, estimated_value=value, approximate=approximate)
        return out

    if isinstance(wanted, bool):
        if param["quantized"] and high - low == 1:
            return raw(high if wanted else low)
        raise RegistryError("%s is not an on/off switch" % param["name"])
    if isinstance(wanted, (int, float)):
        return raw(float(wanted))
    if isinstance(wanted, dict):
        if set(wanted) == {"raw"} and isinstance(wanted["raw"], (int, float)):
            return raw(float(wanted["raw"]))
        if set(wanted) == {"normalized"} and isinstance(wanted["normalized"], (int, float)) \
                and not isinstance(wanted["normalized"], bool) and 0 <= wanted["normalized"] <= 1:
            return raw(low + (high - low) * float(wanted["normalized"]), approximate=param["quantized"])
        raise RegistryError('value objects are {"normalized": 0..1} or {"raw": number}')
    if not isinstance(wanted, str):
        raise RegistryError("%s: unsupported value %r" % (param["name"], wanted))
    text = wanted.strip()
    if param["quantized"]:
        items = param.get("items") or []
        exact = [i for i, item in enumerate(items) if item == text]
        loose = [i for i, item in enumerate(items) if item.lower() == text.lower()]
        prefix = [i for i, item in enumerate(items) if item.lower().startswith(text.lower())]
        for matches in (exact, loose, prefix):
            if len(matches) == 1:
                return raw(low + matches[0])
            if len(matches) > 1 and matches is not prefix:
                break
        raise RegistryError("%s: %r is not one of %s" % (param["name"], wanted, _items_hint(param)))
    parsed = parse_display(text)
    unit = param.get("unit")
    if parsed is None:
        raise RegistryError("%s: cannot read %r; give a number with a unit%s" % (
            param["name"], wanted, (" (%s)" % unit) if unit else ""))
    number, given = parsed
    if unit is None:
        raise RegistryError("%s has no known display unit; give a number (raw %r..%r) or {\"normalized\": x}" % (
            param["name"], low, high))
    if given == "" and unit != "":
        raise RegistryError("%s needs a unit: %s" % (param["name"], unit))
    if given != unit:
        raise RegistryError("%s is measured in %s, not %s" % (param["name"], unit or "plain numbers", given or "plain numbers"))
    lo, hi = param["display_min"], param["display_max"]
    tolerance = max(abs(lo), abs(hi), 1.0) * 1e-3 if math.isfinite(lo) and math.isfinite(hi) else 1e-6
    if not (lo - tolerance <= number <= hi + tolerance):
        raise RegistryError("%s: %s outside %s..%s" % (
            param["name"], text, _fmt(lo, unit), _fmt(hi, unit)))
    curve = param.get("curve") or []
    estimate = curve_to_raw(curve, unit, min(max(number, lo), hi)) if param.get("monotonic") else None
    exact_in_live = (unit in LIVE_PARSEABLE_UNITS and param.get("monotonic")
                     and all(math.isfinite(n) for _, n in curve) and math.isfinite(number))
    if exact_in_live:
        out.update(display_value=format_for_live(min(max(number, lo), hi), unit),
                   estimated_value=estimate, approximate=False)
        return out
    if estimate is None:
        raise RegistryError("%s has a non-monotonic display; give a raw number or {\"normalized\": x}" % param["name"])
    return raw(estimate, approximate=True)


def _fmt(number: float, unit: str) -> str:
    return ("%g %s" % (number, unit)).strip()


def _items_hint(param: dict[str, Any]) -> str:
    items = param.get("items") or []
    shown = ", ".join(items[:16]) + (", ..." if len(items) > 16 else "")
    return "[" + shown + "]"


# ----------------------------------------------------------------- cache ---

def default_registry_dir() -> Path:
    override = os.environ.get("ABLETON_MCP_REGISTRY_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "ableton-mcp" / "device-registry"


class DeviceRegistry:
    """File cache of entries plus in-memory identities of devices seen live.

    Layout: <dir>/<live_version or "unknown">/<key>.json and <dir>/aliases.json
    (optional user aliases: {"*": {"brightness": "Filter Freq"},
    "<ClassName>": {...}}).
    """

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory else default_registry_dir()
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}
        # (track_type, track_index, device_path) -> live identities from the last scan
        self._locations: dict[tuple[str, int, tuple[int, ...]], dict[str, Any]] = {}

    # files
    def _path(self, live_version: str | None, key: str) -> Path:
        return self.directory / (live_version or "unknown") / (key + ".json")

    def get(self, live_version: str | None, key: str) -> dict[str, Any] | None:
        with self._lock:
            cached = self._entries.get((live_version or "unknown", key))
            if cached is not None:
                return cached
            path = self._path(live_version, key)
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                validate_entry(entry)
            except (OSError, ValueError):
                return None
            self._entries[(live_version or "unknown", key)] = entry
            return entry

    def put(self, entry: dict[str, Any]) -> Path:
        validate_entry(entry)
        with self._lock:
            path = self._path(entry.get("live_version"), entry["key"])
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, indent=1, allow_nan=True)
            os.replace(tmp, path)
            self._entries[(entry.get("live_version") or "unknown", entry["key"])] = entry
            return path

    def list_entries(self) -> list[dict[str, Any]]:
        rows = []
        if not self.directory.is_dir():
            return rows
        for path in sorted(self.directory.glob("*/*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                validate_entry(entry)
            except (OSError, ValueError):
                continue
            rows.append({"key": entry["key"], "class_name": entry["class_name"],
                         "device_name": entry.get("device_name"), "live_version": entry.get("live_version"),
                         "parameters": len(entry["parameters"]), "uri": "ableton://devices/" + entry["key"]})
        return rows

    def find(self, key: str) -> dict[str, Any] | None:
        """Newest cached entry for a key regardless of Live version."""
        if not self.directory.is_dir():
            return None
        candidates = sorted(self.directory.glob("*/%s.json" % key), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                validate_entry(entry)
                return entry
            except (OSError, ValueError):
                continue
        return None

    def user_aliases(self) -> dict[str, Any]:
        try:
            data = json.loads((self.directory / "aliases.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    # locations
    def remember(self, scan: dict[str, Any], key: str) -> None:
        loc = (scan["track_type"], scan["track_index"], tuple(scan["device_path"]))
        with self._lock:
            self._locations[loc] = {
                "key": key, "live_version": scan.get("live_version"), "class_name": scan["class_name"],
                "target_id": scan["target_id"], "device_id": scan["device_id"],
                "parameter_ids": {p["index"]: p["parameter_id"] for p in scan["parameters"]},
            }

    def location(self, track_type: str, track_index: int, path: list[int]) -> dict[str, Any] | None:
        with self._lock:
            return self._locations.get((track_type, track_index, tuple(path)))

    def forget(self, track_type: str, track_index: int, path: list[int]) -> None:
        with self._lock:
            self._locations.pop((track_type, track_index, tuple(path)), None)


class RegistryClient:
    """Glue between the registry and a send_command(command, params) callable."""

    STALE_CODES = {"stale_target", "identity_required"}

    def __init__(self, send_command, registry: DeviceRegistry | None = None,
                 sample_points: int = DEFAULT_SAMPLE_POINTS):
        self.send = send_command
        self.registry = registry or DeviceRegistry()
        self.sample_points = sample_points

    def ensure(self, track_type: str, track_index: int, path: list[int], refresh: bool = False) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Return (entry, location, info) for a device, scanning only if needed.

        A light scan (no samples) refreshes identities and computes the key;
        the full sampled scan runs only when no cached entry exists.
        """
        selector = {"track_type": track_type, "track_index": track_index, "device_path": list(path)}
        info = {"scans": []}
        location = None if refresh else self.registry.location(track_type, track_index, path)
        if location:
            entry = self.registry.get(location["live_version"], location["key"])
            if entry is not None:
                return entry, location, info
        scan = self.send("scan_device", dict(selector, sample_points=0))
        info["scans"].append("light")
        names = [p["name"] for p in scan["parameters"]]
        key = registry_key(scan["class_name"], names)
        entry = None if refresh else self.registry.get(scan.get("live_version"), key)
        if entry is None:
            scan = self.send("scan_device", dict(selector, sample_points=self.sample_points))
            info["scans"].append("full")
            entry = build_entry(scan)
            info["cached_to"] = str(self.registry.put(entry))
            key = entry["key"]
        self.registry.remember(scan, key)
        return entry, self.registry.location(track_type, track_index, path), info

    def plan(self, entry: dict[str, Any], changes: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(changes, dict) or not changes:
            raise RegistryError('changes must be an object like {"Filter Freq": "800 Hz"}')
        aliases = self.registry.user_aliases()
        planned, errors, seen = [], [], {}
        for query, wanted in changes.items():
            try:
                param = resolve_parameter(entry, int(query) if isinstance(query, str) and query.isdigit() else query, aliases)
                if param["index"] in seen:
                    raise RegistryError("%r and %r both mean %s" % (seen[param["index"]], query, param["name"]))
                seen[param["index"]] = query
                change = plan_change(param, wanted)
                change["requested"] = {"parameter": query, "value": wanted}
                planned.append(change)
            except RegistryError as error:
                errors.append(str(error))
        if errors:
            raise RegistryError("; ".join(errors))
        return planned

    def set_parameters(self, track_type: str, track_index: int, path: list[int], changes: dict[str, Any],
                       dry_run: bool = False) -> dict[str, Any]:
        entry, location, info = self.ensure(track_type, track_index, path)
        planned = self.plan(entry, changes)
        summary = [{"parameter": c["expected_name"], "index": c["parameter_index"],
                    "requested": c["requested"]["value"],
                    "send": c.get("display_value", c.get("value")),
                    "estimated_raw": c.get("estimated_value"), "approximate": c["approximate"]}
                   for c in planned]
        if dry_run:
            return {"dry_run": True, "device": entry["class_name"], "registry_key": entry["key"],
                    "plan": summary, "scans": info["scans"]}
        for attempt in (0, 1):
            wire = [{k: v for k, v in c.items() if k in ("parameter_index", "expected_name", "value", "display_value")}
                    for c in planned]
            for change in wire:
                pid = location["parameter_ids"].get(change["parameter_index"])
                if pid:
                    change["parameter_id"] = pid
            params = {"track_type": track_type, "track_index": track_index, "device_path": list(path),
                      "target_id": location["target_id"], "device_id": location["device_id"],
                      "expected_class": entry["class_name"], "changes": wire}
            try:
                result = self.send("set_device_parameters", params)
                break
            except Exception as error:
                code = (getattr(error, "error", None) or {}).get("code")
                if attempt or code not in self.STALE_CODES:
                    raise
                # Live objects moved or the script reloaded: rescan and replan once.
                self.registry.forget(track_type, track_index, path)
                entry, location, more = self.ensure(track_type, track_index, path)
                info["scans"] += more["scans"]
                info["retried_after"] = code
                planned = self.plan(entry, changes)
        out = {"device": entry["class_name"], "registry_key": entry["key"], "plan": summary,
               "changed": result.get("changed", result), "scans": info["scans"]}
        if "retried_after" in info:
            out["retried_after"] = info["retried_after"]
        if any(c["approximate"] for c in planned):
            out["note"] = "Some values were converted from the cached curve and are approximate; check 'display' in changed."
        return out
