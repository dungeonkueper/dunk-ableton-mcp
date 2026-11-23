# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
import socket
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
from .dataset import dataset_enabled, get_recorder
from .dataset.trajectory_decorator import trajectory_tool

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# 2.0 commands that change Live's state; they get the modifying-command
# socket timeout (mirrors V2_MAIN_THREAD_COMMANDS in the Remote Script).
V2_MODIFYING_COMMANDS = frozenset({
    "set_track_mixer", "delete_track", "duplicate_track",
    "delete_return_track", "create_scene", "delete_scene", "duplicate_scene",
    "fire_scene", "set_scene_properties", "set_clip_properties", "edit_clip",
    "create_arrangement_clip", "delete_arrangement_clip", "insert_device",
    "set_scale", "set_transport", "transport_action", "undo", "redo",
})

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

class AbletonCommandError(Exception):
    """The Remote Script received the command and reported an error.

    The connection itself is fine, so it is kept open, and the script's
    message is passed on as-is rather than as a communication failure.
    """


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception as e:
                    logger.error(f"Error disconnecting from Ableton: {str(e)}")
                finally:
                    self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        """
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "delete_clip",
            "clear_notes_from_clip",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement",
            "create_locator",
            "create_return_track", "set_send",
            "set_clip_automation", "clear_clip_automation",
        ] or command_type in V2_MODIFYING_COMMANDS

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise AbletonCommandError(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except AbletonCommandError:
            raise
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        script_info = None
        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
            from .script_handshake import handshake

            script_info = handshake(ableton.send_command)
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        if dataset_enabled():
            try:
                recorder = get_recorder()
                if recorder:
                    logger.info(
                        "Dataset recording enabled → Supabase session %s",
                        recorder.session_id,
                    )
                from .script_handshake import script_has_capability
                from .dataset.passive_poller import start_passive_poller

                # Runs even without Live-UI events: it also settles implicit
                # preferences for agent actions.
                start_passive_poller()
                if not script_has_capability("drain_passive_events"):
                    logger.warning(
                        "Passive Live listeners unavailable — Remote Script outdated "
                        "or not loaded. Restart Ableton after script update."
                    )
            except Exception as e:
                logger.warning(f"Failed to start dataset recorder: {e}")
        else:
            reason = "unknown"
            try:
                from .config import telemetry_config  # noqa: F401
                from .telemetry import get_telemetry_consent, is_telemetry_enabled

                if not is_telemetry_enabled():
                    reason = "telemetry disabled (config.enabled=False or DISABLE_TELEMETRY)"
                elif not get_telemetry_consent():
                    reason = "no dataset consent yet — the user has not opted in"
                else:
                    reason = "ABLETON_MCP_DISABLE_DATASET or unexpected gate failure"
            except Exception:
                reason = "MCP_Server/config.py missing or unreadable (gitignored; required for Supabase)"
            logger.info("Dataset recording off — %s", reason)

        yield {"script_info": script_info}
    finally:
        global _ableton_connection
        try:
            from .dataset.passive_poller import stop_passive_poller

            stop_passive_poller()
        except Exception as e:
            logger.debug(f"Failed to stop passive poller: {e}")
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        try:
            recorder = get_recorder()
            if recorder:
                # Bounded wait so queued rows land before the daemon worker
                # is killed at exit.
                recorder.end(timeout=float(
                    os.environ.get("ABLETON_MCP_DATASET_FLUSH_SEC", "5")
                ))
        except Exception as e:
            logger.debug(f"Failed to end dataset session: {e}")
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            # Passive events stop arriving while disconnected, so any cached
            # state hash may describe a Live session that has since moved on.
            try:
                recorder = get_recorder()
                if recorder:
                    recorder.invalidate_cached_state()
            except Exception:
                pass
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
def set_dataset_consent(ctx: Context, consent: bool, user_said: str = "") -> str:
    """Record the user's answer to the dataset consent question.

    Call this ONLY after the user has answered in their own words. Never infer
    the answer, never call it on their behalf, and never call it because
    contributing seems helpful — consent the user did not give is not consent.
    If they have not been asked yet, ask first and wait for their reply.

    Parameters:
    - consent: True if the user agreed to contribute, False if they declined
    - user_said: The user's reply, quoted as closely as possible
    """
    # Intentionally undecorated: recording this call would mean collecting data
    # from someone who may have just declined.
    try:
        from .dataset.consent import record_consent

        state = record_consent(consent, quote=user_said)
    except Exception as e:
        logger.error(f"Could not record dataset consent: {str(e)}")
        return f"Error recording consent: {str(e)}"

    if not consent:
        return (
            "Recorded: dataset contribution declined. Nothing is uploaded, and "
            "you will not be asked again."
        )

    try:
        from .telemetry import refresh_consent_from_dataset

        refresh_consent_from_dataset()
        recorder = get_recorder()
        # Under opt-in the poller is almost never started at boot — consent did
        # not exist yet — so a grant has to start it here or passive Live events
        # would be missed for the rest of the session. Idempotent and
        # self-gating, so this is safe even if startup did start it.
        from .dataset.passive_poller import start_passive_poller

        start_passive_poller()
    except Exception as e:
        logger.error(f"Consent saved but recording failed to start: {str(e)}")
        return (
            "Consent saved, but recording could not start in this session. "
            "It will begin after a restart."
        )

    if recorder is None:
        return (
            "Consent saved, but recording is unavailable (telemetry may be "
            f"disabled). State: {state}."
        )
    return (
        "Thank you — this session is now being contributed to the open dataset. "
        "Say so at any time to stop, or set ABLETON_MCP_DISABLE_DATASET=1."
    )


@mcp.tool()
@telemetry_tool("get_session_info")
@trajectory_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_remote_script_info")
@trajectory_tool("get_remote_script_info", modifying=False)
def get_remote_script_info(ctx: Context, user_prompt: str = "") -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
        from .script_handshake import get_cached_script_info, handshake

        ableton = get_ableton_connection()
        info = handshake(ableton.send_command)
        cached = dict(get_cached_script_info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return json.dumps(cached, indent=2)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
@trajectory_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Each clip slot's clip includes midi_clip_type (Live 13+): 0 = normal MIDI
    clip (piano roll), 1 = Step Sequencer clip, null = not a MIDI clip or a
    Live version without the attribute. Check it before writing notes:
    add_notes_to_clip and clear_notes_from_clip refuse any clip whose
    midi_clip_type is not 0/null. If the type exists but cannot be read,
    midi_clip_type is null and midi_clip_type_error explains why; writes to
    such a clip are refused too.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_clip_notes")
@trajectory_tool("get_clip_notes")
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Returns pitch, start_time, duration, velocity, mute, plus probability,
    velocity_deviation, release_velocity and note_id when Live provides them.
    The note dicts can be passed straight back to add_notes_to_clip.

    Also returns midi_clip_type (Live 13+; null on older Live) and
    notes_may_not_match_ui. When notes_may_not_match_ui is true (e.g. a Live 13
    Step Sequencer clip) the returned notes are NOT what the user sees or hears:
    the API cannot read the clip's steps, and any notes listed were stored
    invisibly. Do not reason about the clip's musical content from them.
    It is also true, with midi_clip_type_error set, when the clip type could
    not be determined.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_clip_notes")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("get_device_parameters")
@trajectory_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all parameters for a device on a track (name, value, min, max).

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_device_parameters",
            {"track_index": track_index, "device_index": device_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
@trajectory_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_index: int,
    value: float,
    user_prompt: str = "",
) -> str:
    """
    Set a device parameter to a specific value.

    Use get_device_parameters first to discover parameter indices and ranges.

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - parameter_index: Index into device.parameters
    - value: New parameter value (Live parameter units)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_device_parameter",
            {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "value": value,
            },
        )
        return (
            f"Set {result.get('name', 'parameter')} "
            f"{result.get('old_value')} → {result.get('value')}"
        )
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
@telemetry_tool("get_session_snapshot")
@trajectory_tool("get_session_snapshot")
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Capture a full project state snapshot for trajectory recording.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Used to version musical state S_t → S_{t+1}.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_session_snapshot")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@mcp.tool()
@telemetry_tool("create_midi_track")
@trajectory_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_audio_track")
@trajectory_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created new audio track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_return_track")
@trajectory_tool("create_return_track")
def create_return_track(ctx: Context, user_prompt: str = "") -> str:
    """
    Create a new return track (Live API: Song.create_return_track()).

    Like the Live API this takes no index: the return track is appended after
    the existing ones, and every track gets a new send for it at the end of
    its send list. The send index to use with set_send equals the new return
    track's index (send A = 0, send B = 1, ...).

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("create_return_track")
        if missing:
            raise ToolError(missing)
        ableton = get_ableton_connection()
        result = ableton.send_command("create_return_track", {})
        return (
            f"Created return track '{result.get('name', 'unknown')}' "
            f"(return index {result.get('index')}, use send_index "
            f"{result.get('send_index')} with set_send)"
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error creating return track: {str(e)}")
        raise ToolError(f"Error creating return track: {str(e)}") from e


@mcp.tool()
@rich_telemetry_tool("set_send")
@trajectory_tool("set_send")
def set_send(
    ctx: Context,
    track_index: int,
    send_index: int,
    value: float,
    track_type: str = "track",
    user_prompt: str = "",
) -> str:
    """
    Set a send level (Live API: track.mixer_device.sends[send_index].value).

    send_index addresses the return track the send feeds: 0 = send A /
    first return track, 1 = send B, and so on (see return_track_count in
    get_session_info, or the "sends" list in get_session_snapshot).

    Parameters:
    - track_index: Index into Song.tracks, or into Song.return_tracks when
      track_type is "return"
    - send_index: Index into the track's sends (= return track index)
    - value: Send level in Live's parameter units, 0.0 (-inf dB) to 1.0
      (0 dB). Out-of-range values are rejected, not clamped.
    - track_type: "track" (default) for a regular track, "return" to set a
      return track's send to another return track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("set_send")
        if missing:
            raise ToolError(missing)
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_send",
            {
                "track_index": track_index,
                "send_index": send_index,
                "value": value,
                "track_type": track_type,
            },
        )
        target = result.get("return_track_name") or result.get(
            "send_name", f"send {send_index}")
        return (
            f"Set send '{result.get('send_name', send_index)}' on "
            f"'{result.get('track_name', track_index)}' → '{target}': "
            f"{result.get('old_value')} → {result.get('value')}"
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error setting send: {str(e)}")
        raise ToolError(f"Error setting send: {str(e)}") from e


@mcp.tool()
@rich_telemetry_tool("set_clip_automation")
@trajectory_tool("set_clip_automation")
def set_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    points: list[dict],
    parameter_type: str = "device",
    device_index: int | None = None,
    parameter_index: int | None = None,
    send_index: int | None = None,
    clip_location: str = "session",
    resolution: float = 0.25,
    clear_existing: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Write a clip envelope (automation) for one parameter, e.g. a filter sweep.

    Live API: Clip.automation_envelope / create_automation_envelope and
    AutomationEnvelope.insert_step. The envelope belongs to the clip, so it
    plays with the clip. duplicate_to_arrangement usually copies it, but Live
    12.4.5 can drop it on instrument tracks (the tool then warns; check with
    get_clip_automation). Live can only create new envelopes on Session
    clips; on Arrangement clips existing envelopes can be rewritten.
    For a sweep across several arrangement clips, write one segment per clip.

    Ramps between points are written as steps of `resolution` beats holding
    the linearly interpolated value (Live's API only writes steps). Two points
    at the same time make a jump; the last value is held to the clip end.

    Parameters:
    - track_index: Index into Song.tracks
    - clip_index: Session clip slot index, or the index from
      get_arrangement_clips when clip_location is "arrangement"
    - points: [{"time": beats from clip start, "value": parameter units}, ...]
      with non-decreasing times inside the clip. Values outside the
      parameter's min/max are rejected (see get_device_parameters).
    - parameter_type: "device" (needs device_index + parameter_index),
      "volume", "panning" or "send" (needs send_index; 0 = send A)
    - clip_location: "session" (default) or "arrangement"
    - resolution: Step length in beats for ramps (default 0.25 = 1/16 note,
      minimum 0.03125)
    - clear_existing: Remove this parameter's existing envelope first (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("set_clip_automation")
        if missing:
            raise ToolError(missing)
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_clip_automation",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "points": points,
                "parameter_type": parameter_type,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "send_index": send_index,
                "clip_location": clip_location,
                "resolution": resolution,
                "clear_existing": clear_existing,
            },
        )
        check = ", ".join(
            f"{c.get('time')}: {round(float(c.get('value', 0)), 4)}"
            for c in result.get("check", [])
        )
        return (
            f"Wrote automation for '{result.get('parameter', parameter_type)}' "
            f"on {result.get('clip_location', clip_location)} clip "
            f"'{result.get('clip_name', clip_index)}' "
            f"({result.get('step_count')} steps over "
            f"{result.get('clip_length')} beats)"
            + (f"; envelope at points: {check}" if check else "")
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error setting clip automation: {str(e)}")
        raise ToolError(f"Error setting clip automation: {str(e)}") from e


@mcp.tool()
@rich_telemetry_tool("clear_clip_automation")
@trajectory_tool("clear_clip_automation")
def clear_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    parameter_type: str = "all",
    device_index: int | None = None,
    parameter_index: int | None = None,
    send_index: int | None = None,
    clip_location: str = "session",
    user_prompt: str = "",
) -> str:
    """
    Remove clip envelopes (Live API: Clip.clear_envelope / clear_all_envelopes).

    Parameters:
    - track_index, clip_index, clip_location: as in set_clip_automation
    - parameter_type: "all" (default, every envelope in the clip), or
      "device" / "volume" / "panning" / "send" with the same extra indices
      as set_clip_automation to clear one parameter
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("clear_clip_automation")
        if missing:
            raise ToolError(missing)
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "clear_clip_automation",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "parameter_type": parameter_type,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "send_index": send_index,
                "clip_location": clip_location,
            },
        )
        return (
            f"Cleared automation ({result.get('cleared', parameter_type)}) on "
            f"{result.get('clip_location', clip_location)} clip "
            f"'{result.get('clip_name', clip_index)}'"
        )
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error clearing clip automation: {str(e)}")
        raise ToolError(f"Error clearing clip automation: {str(e)}") from e


# ══ 2.0 tools ════════════════════════════════════════════════════════════
#
# Each modifying 2.0 command runs inside one Live undo step, so one Cmd+Z (or
# the undo tool) reverts a whole tool call.


def _v2_command(command: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    """Send a 2.0 command after the capability check; errors become ToolError.

    None values are dropped so the Remote Script only sees what the caller set.
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability(command)
        if missing:
            raise ToolError(missing)
        ableton = get_ableton_connection()
        clean = {k: v for k, v in params.items() if v is not None}
        return ableton.send_command(command, clean)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error {what}: {str(e)}")
        raise ToolError(f"Error {what}: {str(e)}") from e


def _json(result: Any) -> str:
    return json.dumps(result, indent=2)


# ── Mixer and tracks ────────────────────────────────────────────────────────

@mcp.tool()
@rich_telemetry_tool("set_track_mixer")
@trajectory_tool("set_track_mixer")
def set_track_mixer(
    ctx: Context,
    track_index: int = 0,
    volume: float | None = None,
    panning: float | None = None,
    mute: bool | None = None,
    solo: bool | None = None,
    arm: bool | None = None,
    monitoring: str | None = None,
    color_index: int | None = None,
    track_type: str = "track",
    user_prompt: str = "",
) -> str:
    """
    Set a track's mixer: volume, pan, mute, solo, arm, monitoring, color.

    Only the arguments you pass are changed. Values are validated before
    anything is written, so a bad value changes nothing.

    Parameters:
    - track_index: Index into Song.tracks (or return_tracks for track_type
      "return"; ignored for "master")
    - volume: Fader in Live's units, 0.0 to 1.0. 0.85 is 0 dB, 1.0 is +6 dB,
      0.0 is -inf. The result shows the dB value Live displays.
    - panning: -1.0 (left) to 1.0 (right), 0 = center
    - mute, solo: true/false (not on the master track)
    - arm: true/false (regular MIDI/audio tracks only)
    - monitoring: "in", "auto" or "off" (regular tracks only)
    - color_index: Live color palette index 0-69
    - track_type: "track" (default), "return" or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("set_track_mixer", {
        "track_index": track_index, "track_type": track_type,
        "volume": volume, "panning": panning, "mute": mute, "solo": solo,
        "arm": arm, "monitoring": monitoring, "color_index": color_index,
    }, "setting track mixer")
    parts = []
    for key, change in result.get("changes", {}).items():
        new = change.get("new")
        if isinstance(new, dict):
            new = new.get("display") or new.get("value")
        parts.append(f"{key} → {new}")
    return f"'{result.get('track_name')}': " + ", ".join(parts)


@mcp.tool()
@telemetry_tool("delete_track")
@trajectory_tool("delete_track")
def delete_track(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Delete a track (Live API: Song.delete_track). Undo restores it.

    Parameters:
    - track_index: Index into Song.tracks. Live keeps at least one track.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("delete_track", {"track_index": track_index},
                         "deleting track")
    return (f"Deleted track {result.get('deleted_index')} "
            f"'{result.get('deleted_name')}' ({result.get('track_count')} tracks left)")


@mcp.tool()
@telemetry_tool("duplicate_track")
@trajectory_tool("duplicate_track")
def duplicate_track(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Duplicate a track with its devices and clips (Live API: Song.duplicate_track).
    The copy is inserted right after the original.

    Parameters:
    - track_index: Index into Song.tracks
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("duplicate_track", {"track_index": track_index},
                         "duplicating track")
    return (f"Duplicated track {track_index} as track {result.get('new_index')} "
            f"'{result.get('new_name')}'")


@mcp.tool()
@telemetry_tool("delete_return_track")
@trajectory_tool("delete_return_track")
def delete_return_track(ctx: Context, return_index: int, user_prompt: str = "") -> str:
    """
    Delete a return track (Live API: Song.delete_return_track). The matching
    send disappears from every track, and later sends shift down by one.

    Parameters:
    - return_index: Index into Song.return_tracks (0 = A)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("delete_return_track", {"return_index": return_index},
                         "deleting return track")
    return (f"Deleted return track '{result.get('deleted_name')}' "
            f"({result.get('return_track_count')} return tracks left)")


# ── Scenes ──────────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("create_scene")
@trajectory_tool("create_scene")
def create_scene(ctx: Context, index: int = -1, name: str = "", user_prompt: str = "") -> str:
    """
    Create a scene (Live API: Song.create_scene).

    Parameters:
    - index: Position to insert at; -1 (default) appends at the end
    - name: Optional scene name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("create_scene", {"index": index, "name": name or None},
                         "creating scene")
    return f"Created scene {result.get('index')} '{result.get('name')}'"


@mcp.tool()
@telemetry_tool("delete_scene")
@trajectory_tool("delete_scene")
def delete_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Delete a scene and its clip slots (Live API: Song.delete_scene).

    Parameters:
    - scene_index: Index into Song.scenes. Live keeps at least one scene.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("delete_scene", {"scene_index": scene_index},
                         "deleting scene")
    return f"Deleted scene {scene_index} '{result.get('deleted_name')}'"


@mcp.tool()
@telemetry_tool("duplicate_scene")
@trajectory_tool("duplicate_scene")
def duplicate_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Duplicate a scene with its clips (Live API: Song.duplicate_scene).
    The copy is inserted right after the original.

    Parameters:
    - scene_index: Index into Song.scenes
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("duplicate_scene", {"scene_index": scene_index},
                         "duplicating scene")
    return f"Duplicated scene {scene_index} as scene {result.get('new_index')}"


@mcp.tool()
@telemetry_tool("fire_scene")
@trajectory_tool("fire_scene")
def fire_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Launch a scene (Live API: Scene.fire).

    Parameters:
    - scene_index: Index into Song.scenes
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("fire_scene", {"scene_index": scene_index},
                         "firing scene")
    return f"Fired scene {scene_index} '{result.get('name')}'"


@mcp.tool()
@rich_telemetry_tool("set_scene_properties")
@trajectory_tool("set_scene_properties")
def set_scene_properties(
    ctx: Context,
    scene_index: int,
    name: str | None = None,
    tempo: float | None = None,
    tempo_enabled: bool | None = None,
    time_signature_numerator: int | None = None,
    time_signature_denominator: int | None = None,
    color_index: int | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set a scene's name, tempo, time signature or color.

    Parameters:
    - scene_index: Index into Song.scenes
    - name: New scene name
    - tempo: Scene tempo in BPM (20-999); also pass tempo_enabled=true so
      launching the scene changes the song tempo
    - tempo_enabled: Whether launching the scene sets the tempo
    - time_signature_numerator / time_signature_denominator: Scene time
      signature (denominator 1, 2, 4, 8 or 16)
    - color_index: Live color palette index 0-69
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("set_scene_properties", {
        "scene_index": scene_index, "name": name, "tempo": tempo,
        "tempo_enabled": tempo_enabled,
        "time_signature_numerator": time_signature_numerator,
        "time_signature_denominator": time_signature_denominator,
        "color_index": color_index,
    }, "setting scene properties")
    return (f"Scene {scene_index} '{result.get('name')}': changed "
            + ", ".join(result.get("changed", [])))


# ── Clips ───────────────────────────────────────────────────────────────────

@mcp.tool()
@rich_telemetry_tool("set_clip_properties")
@trajectory_tool("set_clip_properties")
def set_clip_properties(
    ctx: Context,
    track_index: int,
    clip_index: int,
    clip_location: str = "session",
    name: str | None = None,
    looping: bool | None = None,
    loop_start: float | None = None,
    loop_end: float | None = None,
    start_marker: float | None = None,
    end_marker: float | None = None,
    muted: bool | None = None,
    color_index: int | None = None,
    launch_mode: str | None = None,
    launch_quantization: int | None = None,
    legato: bool | None = None,
    velocity_amount: float | None = None,
    gain: float | None = None,
    pitch_coarse: int | None = None,
    pitch_fine: float | None = None,
    warping: bool | None = None,
    warp_mode: str | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set clip properties: loop and markers, mute, launch settings, color, and
    for audio clips gain, pitch and warping. Only what you pass is changed.

    Positions are in beats from the clip's start, the same time base as notes.

    Parameters:
    - track_index, clip_index: The clip; clip_index is a Session slot, or an
      index from get_arrangement_clips when clip_location is "arrangement"
    - clip_location: "session" (default) or "arrangement"
    - looping: Loop on/off
    - loop_start / loop_end: Loop brace in beats (end must be after start)
    - start_marker / end_marker: Playback start/end markers in beats
    - muted: Clip activator off (true) or on (false)
    - color_index: Live color palette index 0-69
    - launch_mode: "trigger", "gate", "toggle" or "repeat"
    - launch_quantization: 0 = global, 1 = none, 2 = 8 bars, 3 = 4 bars,
      4 = 2 bars, 5 = 1 bar, 6 = 1/2, 7 = 1/2T, 8 = 1/4, 9 = 1/4T, 10 = 1/8,
      11 = 1/8T, 12 = 1/16, 13 = 1/16T, 14 = 1/32
    - legato: Legato launch on/off
    - velocity_amount: How much note velocity affects volume, 0.0-1.0
    - gain: Audio clip gain in Live's units 0.0-1.0 (audio only)
    - pitch_coarse: Semitones -48..48 (audio only)
    - pitch_fine: Cents -50..50 (audio only)
    - warping: Warp on/off (audio only)
    - warp_mode: "beats", "tones", "texture", "re-pitch", "complex" or
      "complex pro" (audio only)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("set_clip_properties", {
        "track_index": track_index, "clip_index": clip_index,
        "clip_location": clip_location, "name": name, "looping": looping,
        "loop_start": loop_start, "loop_end": loop_end,
        "start_marker": start_marker, "end_marker": end_marker,
        "muted": muted, "color_index": color_index,
        "launch_mode": launch_mode, "launch_quantization": launch_quantization,
        "legato": legato, "velocity_amount": velocity_amount, "gain": gain,
        "pitch_coarse": pitch_coarse, "pitch_fine": pitch_fine,
        "warping": warping, "warp_mode": warp_mode,
    }, "setting clip properties")
    return ("Changed " + ", ".join(result.get("changed", []))
            + f" on clip '{result.get('clip', {}).get('name')}'. Now: "
            + _json(result.get("clip", {})))


@mcp.tool()
@rich_telemetry_tool("edit_clip")
@trajectory_tool("edit_clip")
def edit_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    action: str,
    clip_location: str = "session",
    grid: str = "1/16",
    amount: float = 1.0,
    region_start: float | None = None,
    region_length: float | None = None,
    destination_time: float | None = None,
    pitch: int = -1,
    transposition: int = 0,
    user_prompt: str = "",
) -> str:
    """
    Edit a clip with Live's clip commands.

    Actions:
    - "quantize": Quantize the clip's notes to `grid`
      ("1/4", "1/8", "1/8t", "1/8+1/8t", "1/16", "1/16t", "1/16+1/16t",
      "1/32") with strength `amount` 0.0-1.0
    - "crop": Remove everything outside the loop (or the markers)
    - "duplicate_loop": Double the loop and its content
    - "duplicate_region": Copy notes from region_start for region_length
      beats to destination_time (MIDI only; pitch -1 = all notes, else only
      that pitch; transposition in semitones)
    - "duplicate_slot": Copy the Session clip into the next free slot below

    Parameters:
    - track_index, clip_index, clip_location: as in set_clip_properties
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("edit_clip", {
        "track_index": track_index, "clip_index": clip_index,
        "clip_location": clip_location, "action": action, "grid": grid,
        "amount": amount, "region_start": region_start,
        "region_length": region_length, "destination_time": destination_time,
        "pitch": pitch, "transposition": transposition,
    }, "editing clip")
    extra = ""
    if result.get("new_clip_index") is not None:
        extra = f", copy in slot {result['new_clip_index']}"
    return (f"{action} on clip '{result.get('clip_name')}' done{extra}; "
            f"length {result.get('length')}, loop {result.get('loop_start')}"
            f"–{result.get('loop_end')}")


# ── Arrangement clips ───────────────────────────────────────────────────────

@mcp.tool()
@rich_telemetry_tool("create_arrangement_clip", capture_notes=True)
@trajectory_tool("create_arrangement_clip")
def create_arrangement_clip(
    ctx: Context,
    track_index: int,
    start_time: float,
    length: float = 4.0,
    path: str | None = None,
    name: str | None = None,
    notes: list[dict] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Create a clip directly in the Arrangement, without a Session clip
    (Live API: Track.create_midi_clip / Track.create_audio_clip, Live 12).

    On a MIDI track this creates an empty MIDI clip of `length` beats,
    optionally filled with `notes` (same format as add_notes_to_clip). On an
    audio track pass `path` to place an audio file; its length comes from the
    file. Note: the Live API cannot add new automation envelopes to
    Arrangement clips; for clip automation write it on a Session clip and use
    duplicate_to_arrangement.

    Parameters:
    - track_index: Index into Song.tracks
    - start_time: Arrangement position in beats (0 = start; bar 9 in 4/4 = 32)
    - length: MIDI clip length in beats (default 4)
    - path: Absolute path to an audio file (audio tracks only)
    - name: Optional clip name
    - notes: Optional MIDI notes [{pitch, start_time, duration, velocity, ...}]
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("create_arrangement_clip", {
        "track_index": track_index, "start_time": start_time,
        "length": length, "path": path, "name": name, "notes": notes,
    }, "creating arrangement clip")
    kind = "audio" if result.get("is_audio_clip") else "MIDI"
    notes_txt = (f" with {result.get('note_count')} notes"
                 if result.get("note_count") else "")
    return (f"Created {kind} clip '{result.get('name')}' on "
            f"'{result.get('track_name')}' at beats {result.get('start_time')}"
            f"–{result.get('end_time')}{notes_txt} (arrangement clip_index "
            f"{result.get('clip_index')})")


@mcp.tool()
@telemetry_tool("delete_arrangement_clip")
@trajectory_tool("delete_arrangement_clip")
def delete_arrangement_clip(ctx: Context, track_index: int, clip_index: int,
                            user_prompt: str = "") -> str:
    """
    Delete an Arrangement clip (Live API: Track.delete_clip).

    Parameters:
    - track_index: Index into Song.tracks
    - clip_index: Index from get_arrangement_clips
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("delete_arrangement_clip", {
        "track_index": track_index, "clip_index": clip_index,
    }, "deleting arrangement clip")
    return (f"Deleted arrangement clip '{result.get('name')}' at beat "
            f"{result.get('start_time')}")


# ── Automation read-back ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("get_clip_automation")
@trajectory_tool("get_clip_automation", modifying=False)
def get_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    clip_location: str = "session",
    parameter_type: str = "all",
    device_index: int | None = None,
    parameter_index: int | None = None,
    send_index: int | None = None,
    sample_step: float | None = None,
    user_prompt: str = "",
) -> str:
    """
    Read clip automation back (Live API: Clip.automation_envelopes,
    Envelope.events_in_range and value_at_time). Use it to check what
    set_clip_automation wrote, or to see existing automation before editing.

    Returns, per envelope, the parameter (with the indices set_clip_automation
    takes), its min/max, and `points`: breakpoints {time, value} in beats from
    the clip start. Steps written by set_clip_automation read back as pairs;
    runs of equal values are collapsed to their first and last point.

    Parameters:
    - track_index, clip_index, clip_location: as in set_clip_automation
    - parameter_type: "all" (default, every envelope in the clip), or
      "device" / "volume" / "panning" / "send" with the matching indices
    - sample_step: Optional; also return the envelope value every N beats
      (minimum 0.03125)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("get_clip_automation", {
        "track_index": track_index, "clip_index": clip_index,
        "clip_location": clip_location, "parameter_type": parameter_type,
        "device_index": device_index, "parameter_index": parameter_index,
        "send_index": send_index, "sample_step": sample_step,
    }, "reading clip automation")
    return _json(result)


# ── Devices ─────────────────────────────────────────────────────────────────

@mcp.tool()
@rich_telemetry_tool("insert_device")
@trajectory_tool("insert_device")
def insert_device(
    ctx: Context,
    track_index: int,
    device_name: str,
    device_index: int = -1,
    track_type: str = "track",
    user_prompt: str = "",
) -> str:
    """
    Insert a native Live device by name, without searching the browser
    (Live API: Track.insert_device, Live 12.3+). Faster and more predictable
    than load_instrument_or_effect for Live's own instruments and effects.

    Use the name shown in Live's browser, e.g. "Operator", "Drift", "Wavetable",
    "Simpler", "Drum Rack", "EQ Eight", "Compressor", "Auto Filter", "Reverb",
    "Delay", "Saturator", "Utility". Plug-ins, presets and Max for Live
    devices still need load_instrument_or_effect.

    Parameters:
    - track_index: Index into Song.tracks (or return_tracks for "return";
      ignored for "master")
    - device_name: Native device name
    - device_index: Position in the device chain; -1 (default) appends
    - track_type: "track" (default), "return" or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("insert_device", {
        "track_index": track_index, "device_name": device_name,
        "device_index": device_index, "track_type": track_type,
    }, "inserting device")
    return (f"Inserted '{result.get('device_name')}' on "
            f"'{result.get('track_name')}' at device_index "
            f"{result.get('device_index')} ({result.get('parameter_count')} "
            f"parameters; see get_device_parameters)")


# ── Scale ───────────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("read_scale")
@trajectory_tool("read_scale", modifying=False)
def read_scale(ctx: Context, include_available: bool = False, user_prompt: str = "") -> str:
    """
    Read the Live Set's key and scale (Live 12: Song.root_note, scale_name,
    scale_mode, scale_intervals), including the note names in the scale.

    Parameters:
    - include_available: Also list every scale name Live knows
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    return _json(_v2_command("read_scale", {"include_available": include_available},
                             "reading scale"))


@mcp.tool()
@rich_telemetry_tool("set_scale")
@trajectory_tool("set_scale")
def set_scale(
    ctx: Context,
    root_note: str | None = None,
    scale_name: str | None = None,
    scale_mode: bool | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set the Live Set's key and scale (Live 12 Scale Mode).

    Parameters:
    - root_note: Note name ("C", "F#", "Bb") or 0-11 (0 = C)
    - scale_name: Live scale name, e.g. "Major", "Minor", "Dorian",
      "Minor Pentatonic", "Harmonic Minor" (read_scale with
      include_available=true lists all)
    - scale_mode: Turn Scale Mode on (highlights and folds to the scale in
      the MIDI editor) or off
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    root: Any = root_note
    if isinstance(root_note, str) and root_note.strip().isdigit():
        root = int(root_note.strip())
    result = _v2_command("set_scale", {
        "root_note": root, "scale_name": scale_name, "scale_mode": scale_mode,
    }, "setting scale")
    new = result.get("new", {})
    return (f"Scale is now {new.get('root_name')} {new.get('scale_name')} "
            f"(scale mode {'on' if new.get('scale_mode') else 'off'}): "
            + " ".join(new.get("note_names", [])))


# ── Transport and undo ──────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("get_transport")
@trajectory_tool("get_transport", modifying=False)
def get_transport(ctx: Context, user_prompt: str = "") -> str:
    """
    Read the transport: playing state, position, tempo, time signature,
    arrangement loop, metronome, record/overdub/punch, launch quantization,
    swing, song length, undo/redo availability and locators.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    return _json(_v2_command("get_transport", {}, "reading transport"))


@mcp.tool()
@rich_telemetry_tool("set_transport")
@trajectory_tool("set_transport")
def set_transport(
    ctx: Context,
    loop: bool | None = None,
    loop_start: float | None = None,
    loop_length: float | None = None,
    metronome: bool | None = None,
    record_mode: bool | None = None,
    arrangement_overdub: bool | None = None,
    session_record: bool | None = None,
    punch_in: bool | None = None,
    punch_out: bool | None = None,
    signature_numerator: int | None = None,
    signature_denominator: int | None = None,
    clip_trigger_quantization: int | None = None,
    swing_amount: float | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set transport options. Only what you pass is changed.

    Parameters:
    - loop: Arrangement loop on/off
    - loop_start / loop_length: Arrangement loop in beats. Live does not
      place the loop beyond the song end; the result warns when Live
      adjusted a value.
    - metronome: Click on/off
    - record_mode: Arrangement record on/off (starts recording when playing)
    - arrangement_overdub: MIDI arrangement overdub on/off
    - session_record: Session record button on/off
    - punch_in / punch_out: Punch switches
    - signature_numerator / signature_denominator: Song time signature
    - clip_trigger_quantization: Global launch quantization: 0 = none,
      1 = 8 bars, 2 = 4 bars, 3 = 2 bars, 4 = 1 bar, 5 = 1/2, 6 = 1/2T,
      7 = 1/4, 8 = 1/4T, 9 = 1/8, 10 = 1/8T, 11 = 1/16, 12 = 1/16T, 13 = 1/32
    - swing_amount: Global swing for quantizing, 0.0-1.0
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("set_transport", {
        "loop": loop, "loop_start": loop_start, "loop_length": loop_length,
        "metronome": metronome, "record_mode": record_mode,
        "arrangement_overdub": arrangement_overdub,
        "session_record": session_record, "punch_in": punch_in,
        "punch_out": punch_out, "signature_numerator": signature_numerator,
        "signature_denominator": signature_denominator,
        "clip_trigger_quantization": clip_trigger_quantization,
        "swing_amount": swing_amount,
    }, "setting transport")
    state = result.get("state", {})
    text = ("Changed " + ", ".join(result.get("changed", []))
            + f". Loop {'on' if state.get('loop') else 'off'} "
            f"{state.get('loop_start')}+{state.get('loop_length')} beats, "
            f"metronome {'on' if state.get('metronome') else 'off'}, "
            f"{state.get('signature_numerator')}/{state.get('signature_denominator')}")
    for warning in result.get("warnings", []):
        text += f"\nWarning: {warning}"
    return text


@mcp.tool()
@telemetry_tool("transport_action")
@trajectory_tool("transport_action")
def transport_action(
    ctx: Context,
    action: str,
    beats: float | None = None,
    quantized: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Run a transport command.

    Actions:
    - "continue_playing": Resume from the current position
    - "stop_all_clips": Stop all Session clips (quantized unless quantized=false)
    - "jump_by": Move the playhead by `beats` (negative = back)
    - "jump_to_next_cue" / "jump_to_prev_cue": Go to the next/previous locator
    - "tap_tempo": One tempo tap
    - "capture_midi": Capture MIDI that was just played into a clip
    - "re_enable_automation": Re-enable automation overridden by hand

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("transport_action", {
        "action": action, "beats": beats, "quantized": quantized,
    }, "running transport action")
    return (f"{action} done; position {result.get('current_song_time')}, "
            f"{'playing' if result.get('is_playing') else 'stopped'}")


@mcp.tool()
@telemetry_tool("undo")
@trajectory_tool("undo", modifying=False)
def undo(ctx: Context, user_prompt: str = "") -> str:
    """
    Undo the last change in Live (Song.undo). Every modifying tool of this
    server is one undo step, so this reverts the last whole tool call.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("undo", {}, "undoing")
    return (f"Undone. can_undo={result.get('can_undo')}, "
            f"can_redo={result.get('can_redo')}")


@mcp.tool()
@telemetry_tool("redo")
@trajectory_tool("redo", modifying=False)
def redo(ctx: Context, user_prompt: str = "") -> str:
    """
    Redo the last undone change in Live (Song.redo).

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    result = _v2_command("redo", {}, "redoing")
    return (f"Redone. can_undo={result.get('can_undo')}, "
            f"can_redo={result.get('can_redo')}")


@mcp.tool()
@rich_telemetry_tool("set_track_name")
@trajectory_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
@trajectory_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
@trajectory_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
@trajectory_tool("add_notes_to_clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a Session clip. Writes are additive; use
    clear_notes_from_clip first to replace the clip's contents.

    Note fields (only pitch is required; defaults in brackets):
    - pitch: integer 0..127
    - start_time: beats from clip start [0.0]
    - duration: beats, > 0 [0.25]
    - velocity: 1..127 [100]
    - mute: boolean [false]
    - probability: 0.0..1.0 [1.0]
    - velocity_deviation: -127..127 [0.0]
    - release_velocity: 0..127 [64]
    Other keys (e.g. note_id from get_clip_notes) are ignored. Out-of-range
    values fail the whole call; nothing is clamped and nothing is written.

    Fails with an error (and leaves the clip unchanged) when the clip is a
    Live 13 Step Sequencer clip (midi_clip_type != 0), or when its type cannot
    be determined: Live accepts such notes but never shows or plays them. The user must convert the clip to MIDI
    first. get_track_info shows each clip's midi_clip_type.

    The result reports api_used: "add_new_notes" (all fields stored) or
    "set_notes" (old Live; probability/velocity_deviation/release_velocity
    are dropped and listed as ignored).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries as described above
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        raise ToolError(f"Error adding notes to clip: {str(e)}") from e
    return _format_add_notes_result(result, notes, track_index, clip_index)


def _format_add_notes_result(result, notes, track_index: int, clip_index: int) -> str:
    result = result if isinstance(result, dict) else {}
    count = result.get("note_count", len(notes))
    api_used = result.get("api_used", "unknown")
    midi_clip_type = result.get("midi_clip_type")
    text = (
        f"Added {count} note(s) to clip at track {track_index}, slot {clip_index} "
        f"(api_used={api_used}, midi_clip_type="
        f"{'null' if midi_clip_type is None else midi_clip_type})"
    )
    ignored = result.get("ignored_fields")
    if ignored:
        text += (
            ". Warning: this Live version only supports the legacy set_notes API; "
            f"these fields were not stored: {', '.join(ignored)}"
        )
    return text

@mcp.tool()
@rich_telemetry_tool("clear_notes_from_clip")
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = ""
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Fails with an error (and leaves the clip unchanged) for a Live 13 Step
    Sequencer clip (midi_clip_type != 0) or a clip whose type cannot be read.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("clear_notes_from_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        raise ToolError(f"Error clearing notes from clip: {str(e)}") from e

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
@trajectory_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_arrangement_clip_name")
@trajectory_tool("set_arrangement_clip_name")
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
@trajectory_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
@trajectory_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
@trajectory_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
@trajectory_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("delete_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("start_playback")
@trajectory_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
@trajectory_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
@trajectory_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
@trajectory_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
@trajectory_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
@trajectory_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
@trajectory_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
@trajectory_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
@trajectory_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        text = (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
        if result.get("warning"):
            text += f"\nWarning: {result['warning']}"
        return text
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("create_locator")
def create_locator(
    ctx: Context,
    name: str,
    time: float,
    user_prompt: str = ""
) -> str:
    """
    Create a named locator (cue point) in the Arrangement at a beat position.

    If a locator already exists at that beat (within ~1e-3 tolerance) it is
    renamed instead of toggled off. Time is in beats from the start of the
    arrangement (e.g. 0.0 = start, 16.0 = bar 5 in 4/4).

    Parameters:
    - name: The locator label (e.g. "Chorus", "Verse 1", "Drop")
    - time: Beat position where the locator should sit
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("create_locator")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "create_locator",
            {"name": name, "time": time}
        )
        return (
            f"Locator '{result.get('name', name)}' set at beat "
            f"{result.get('time', time)}"
        )
    except Exception as e:
        logger.error(f"Error creating locator: {str(e)}")
        return f"Error creating locator: {str(e)}"


# ── Dataset / preference tools (Supabase trajectory recording) ─────────────────

@mcp.tool()
@telemetry_tool("submit_intent")
@trajectory_tool("submit_intent", modifying=False)
def submit_intent(
    ctx: Context,
    text: str,
    level: int = 5,
    user_prompt: str = "",
) -> str:
    """
    Record the human's creative intent for subsequent actions in this session.

    Call this before a sequence of edits so trajectory steps are conditioned on intent.
    Levels: 1=atomic, 2=musical op, 3=section, 4=song, 5=creative goal.

    Requires telemetry consent. Data is stored in Supabase (same project as telemetry).

    Parameters:
    - text: Natural-language intent (e.g. "make the chorus feel bigger")
    - level: Hierarchical intent level 1–5 (default 5)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.set_intent(text=text, level=level)
        return (
            f"Recorded intent {event.intent_id} (level {level}): {text!r}. "
            f"Session: {recorder.session_id}"
        )
    except Exception as e:
        logger.error(f"Error submitting intent: {str(e)}")
        return f"Error submitting intent: {str(e)}"


@mcp.tool()
@telemetry_tool("rate_last_action")
@trajectory_tool("rate_last_action", modifying=False)
def rate_last_action(
    ctx: Context,
    rating: str,
    tags: str = "",
    note: str = "",
    user_prompt: str = "",
) -> str:
    """
    Rate the most recent recorded action (or the track after an edit).

    Ratings: better | same | worse | keep | reject | thumbs_up | thumbs_down
    Optional tags (comma-separated): groove, harmony, melody, sound, arrangement,
    energy, mix, emotion

    Requires telemetry consent.

    Parameters:
    - rating: Preference label
    - tags: Comma-separated aspect tags
    - note: Optional free-text reason
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        event = recorder.record_preference(
            rating=rating.strip().lower(),
            tags=tag_list,
            note=note or None,
        )
        target = event.target_action_id or "none"
        return f"Recorded preference {rating!r} for action {target}"
    except Exception as e:
        logger.error(f"Error rating action: {str(e)}")
        return f"Error rating action: {str(e)}"


@mcp.tool()
@telemetry_tool("prefer_candidate")
@trajectory_tool("prefer_candidate", modifying=False)
def prefer_candidate(
    ctx: Context,
    candidate_a: str,
    candidate_b: str,
    winner: str,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Record a pairwise preference between two candidate actions or auditions.

    winner should be 'a', 'b', or an explicit candidate id matching A or B.

    Requires telemetry consent.

    Parameters:
    - candidate_a: Id / URI / label for option A
    - candidate_b: Id / URI / label for option B
    - winner: 'a', 'b', or the winning id
    - reason: Optional reason ("C has the right attack")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        w = winner.strip().lower()
        if w == "a":
            w = candidate_a
        elif w == "b":
            w = candidate_b
        event = recorder.record_preference(
            rating="pairwise",
            winner=w,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            note=reason or None,
        )
        return f"Recorded pairwise preference: winner={w!r} (event {event.event_id})"
    except Exception as e:
        logger.error(f"Error recording pairwise preference: {str(e)}")
        return f"Error recording pairwise preference: {str(e)}"


@mcp.tool()
@telemetry_tool("reject_last_action")
@trajectory_tool("reject_last_action", modifying=False)
def reject_last_action(
    ctx: Context,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Mark the last action as rejected (exploration / preference boundary).

    Use when the human undoes or discards an agent edit. Requires telemetry consent.

    Parameters:
    - reason: Optional why it was rejected
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_preference(
            rating="reject",
            note=reason or None,
        )
        return f"Recorded rejection for action {event.target_action_id or 'none'}"
    except Exception as e:
        logger.error(f"Error rejecting action: {str(e)}")
        return f"Error rejecting action: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("record_audition")
@trajectory_tool("record_audition", modifying=False)
def record_audition(
    ctx: Context,
    uri: str,
    kept: bool = False,
    search_query: str = "",
    dwell_ms: float = 0.0,
    user_prompt: str = "",
) -> str:
    """
    Log a browser/preset/sample audition (keep or reject) for preference learning.

    Call once per candidate auditioned. Does not load the device — use
    load_instrument_or_effect when kept=True and you want to commit.

    Requires telemetry consent.

    Parameters:
    - uri: Browser item URI (or stable preset/sample id)
    - kept: Whether this candidate was kept
    - search_query: Optional search text that led here (e.g. "analog bass")
    - dwell_ms: Optional time spent auditioning
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_audition(
            uri=uri,
            kept=kept,
            search_query=search_query or None,
            dwell_ms=dwell_ms if dwell_ms > 0 else None,
        )
        status = "kept" if kept else "rejected"
        return f"Recorded audition ({status}) uri={uri!r} event={event.event_id}"
    except Exception as e:
        logger.error(f"Error recording audition: {str(e)}")
        return f"Error recording audition: {str(e)}"



# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()