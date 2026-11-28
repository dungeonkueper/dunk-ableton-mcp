"""MCP tools and resources for the local device registry (fork prototype)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from .dataset.trajectory_decorator import trajectory_tool
from .device_registry import DeviceRegistry, RegistryClient, RegistryError, compact_entry, jsonable
from .telemetry_decorator import rich_telemetry_tool

_registry = DeviceRegistry()


def _path(device_index: int, device_path: list[int] | None) -> list[int]:
    return list(device_path) if device_path else [device_index]


def register_registry_tools(mcp, get_connection, registry: DeviceRegistry | None = None):
    registry = registry or _registry

    def client() -> RegistryClient:
        return RegistryClient(get_connection().send_command, registry)

    def require(*commands: str) -> None:
        from .script_handshake import require_capability
        get_connection()
        for command in commands:
            missing = require_capability(command)
            if missing:
                raise ToolError(missing)

    @mcp.tool(name="scan_device")
    @rich_telemetry_tool("scan_device")
    @trajectory_tool("scan_device", modifying=False)
    async def scan_device(ctx: Context, track_index: int, device_index: int = 0, track_type: str = "track",
                          device_path: list[int] | None = None, refresh: bool = False, user_prompt: str = "") -> str:
        """Learn a device's parameters (names, units, real-unit ranges, steps).

        Reads only; the result is cached locally per device class and Live
        version, so later calls and set_device_parameters_by_name need no
        listing. Usually unnecessary: set_device_parameters_by_name scans on
        first contact by itself. refresh=True rescans.
        """
        require("scan_device")
        try:
            entry, _, info = await asyncio.to_thread(
                client().ensure, track_type, track_index, _path(device_index, device_path), refresh)
            return json.dumps(dict(compact_entry(entry), scans=info["scans"]), allow_nan=False)
        except Exception as error:
            raise ToolError(str(error)) from error

    @mcp.tool(name="set_device_parameters_by_name")
    @rich_telemetry_tool("set_device_parameters_by_name")
    @trajectory_tool("set_device_parameters_by_name")
    async def set_device_parameters_by_name(ctx: Context, track_index: int, changes: dict[str, Any],
                                            device_index: int = 0, track_type: str = "track",
                                            device_path: list[int] | None = None, dry_run: bool = False,
                                            user_prompt: str = "") -> str:
        """Set one or more parameters of one device by name, in real units.

        changes maps a parameter to a value, e.g.
        {"Filter Freq": "800 Hz", "resonance": 0.4, "Osc 1 Wave": "Saw",
         "Attack": "12 ms", "Gain": "-6 dB", "Dry/Wet": {"normalized": 0.3}}.
        Parameters: exact name, loose name, common alias (cutoff, attack,
        mix...) or "#index". Values: number = raw Live value; string with unit
        (Hz/kHz, ms/s, dB, %, st, L/R); item name for stepped parameters;
        {"normalized": 0..1}; true/false for switches. All changes are checked
        before anything is written and applied together as one undo step.
        dry_run=True only shows the plan. No device listing needed first.
        """
        require("scan_device", "set_device_parameters")
        try:
            result = await asyncio.to_thread(
                client().set_parameters, track_type, track_index, _path(device_index, device_path), changes, dry_run)
            return json.dumps(jsonable(result), allow_nan=False)
        except RegistryError as error:
            raise ToolError(str(error)) from error
        except Exception as error:
            raise ToolError(str(error)) from error

    @mcp.resource("ableton://devices", name="device_registry_index", mime_type="application/json",
                  description="Devices learned so far (local cache; no Live call).")
    def device_index_resource() -> str:
        return json.dumps(registry.list_entries(), allow_nan=False)

    @mcp.resource("ableton://devices/{key}", name="device_registry_entry", mime_type="application/json",
                  description="Compact parameter schema of one learned device (local cache; no Live call).")
    def device_entry_resource(key: str) -> str:
        entry = registry.find(key)
        if entry is None:
            raise ValueError("Unknown device registry key %r; see ableton://devices" % key)
        return json.dumps(compact_entry(entry), allow_nan=False)
