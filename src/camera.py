from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from importlib.metadata import version as get_installed_version
from pathlib import Path
from libonvif.utils.adapters import find_adapters
from libonvif.utils.server import EventServer
from libonvif.utils.subscriber import SubscriptionManager
from libonvif.devices.camera import Camera, discover, get_camera_by_ip, set_hostname, \
        set_video_encoder_configuration, set_audio_encoder_configuration, camera_from_json, refresh_camera, \
        goto_preset, continuous_move, move_stop, get_local_date_and_time, set_system_date_and_time, \
        get_time_offset, set_preset, get_presets, remove_preset, create_preset_tour, modify_preset_tour, \
        remove_preset_tour, operate_preset_tour, get_preset_tours
from libonvif.datastructures.capabilities import Capabilities, PTZCapabilities
from libonvif.datastructures.ptz import PTZPreset, PresetTour, TourSpot
from libonvif.utils.serialization import to_dict
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation, CancelledElicitation
from pydantic import BaseModel
import os
import sys
import webbrowser
import niquests as requests
from niquests.auth import HTTPDigestAuth
import re
import shutil
import subprocess
from typing import Any

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
logger = logging.getLogger(__name__)

mcp = FastMCP("camera")

USER_AGENT = "camera-app/1.0"

class TripTypeResponse(BaseModel):
    value: str

def get_camera_credentials(camera: Camera) -> None:
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")

def on_error(xaddr: str, ex: Exception) -> None:
    logger.debug(f"error: {xaddr} - {ex}")

def camera_filled(camera: Camera) -> None:
    logger.debug(f"Camera Filled: {camera.hostname} : {camera.device_information.serial_number}")

# --- Event listener integration ---
# Bridges the standalone motion_watcher.py prototype (packages/sse) into
# this server, generalized to "event listener" since future work will
# subscribe to event topics beyond just motion. All cameras share ONE
# EventServer (ONVIF push events are just an HTTP POST to whatever URL a
# camera was told during Subscribe - nothing about the protocol requires
# a separate listener per camera), created on first use by whichever
# camera adds its first subscribed event. Each camera gets its own
# SubscriptionManager, since subscriptions (and their resubscribe
# timers) are inherently per-camera.

EVENT_SERVER_PORT = int(os.environ.get("EVENT_SERVER_PORT", "8856"))
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
OPENCLAW_HOOK_URL = os.environ.get("OPENCLAW_HOOK_URL", "http://127.0.0.1:18789/hooks/camera-motion")
OPENCLAW_HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")
# Reserved subdirectory inside OpenClaw's own workspace where the agent
# should save its own copy of the snapshot. $WORKSPACE_DIR is substituted
# by OpenClaw itself when a tool call uses it - not something we resolve
# here. This is separate from SNAPSHOT_DIR above, which is our own local
# copy on this machine.
OPENCLAW_SNAPSHOT_SUBDIR = "camera-events"

# The one shared EventServer instance, or None until the first camera
# adds a subscribed event. Created by _ensure_camera_subscription_entry.
_event_server = None

# Per-camera state, keyed by IP address: {"camera": Camera, "subscription_manager": SubscriptionManager}.
# Populated lazily, the first time a given camera's subscriptions are
# touched. The Camera object here is queried once and then reused
# across resyncs (its subscription_references list is what actually
# tracks live ONVIF subscriptions) - it is NOT refreshed automatically,
# so if a camera's IP/credentials/xaddr genuinely change, its entry here
# would need to be rebuilt (not handled yet - a later concern).
_camera_subscriptions: dict[str, dict] = {}

# In-memory store, keyed by camera IP address, for the set of event
# topics the user wants that camera marked for observation on. Kept
# deliberately separate from _event_server/_camera_subscriptions above:
# those track live ONVIF subscription state (built lazily, in memory
# only), while this needs to hold user preferences for potentially many
# cameras (nothing stored on the Camera object itself persists across
# get_cameras() calls, which rediscovers cameras fresh every time).
#
# This does not survive a server restart - it is reset to empty on
# every process start.
_subscribed_events_by_camera: dict[str, list[str]] = {}

OPENCLAW_CHAT_SESSION_KEY = "agent:main:main"

@mcp.tool()
async def send_message_to_openclaw_chat(
    message: str,
    session_key: str = OPENCLAW_CHAT_SESSION_KEY,
) -> str:
    """
    Inject an assistant message into an OpenClaw WebChat session.

    Args:
        message:
            Text to display in the OpenClaw chat.

        session_key:
            OpenClaw session receiving the message. The normal main-agent
            session is commonly "agent:main:main".

    Returns:
        A status message describing the result.

    Raises:
        ValueError:
            If message or session_key is empty.

        RuntimeError:
            If the OpenClaw CLI cannot be found or the RPC call fails.
    """
    message = message.strip()
    session_key = session_key.strip()

    if not message:
        raise ValueError("message cannot be empty")

    if not session_key:
        raise ValueError("session_key cannot be empty")

    openclaw_executable = shutil.which("openclaw")

    if openclaw_executable is None:
        raise RuntimeError("The openclaw executable was not found in PATH")

    params = json.dumps(
        {
            "sessionKey": session_key,
            "message": message,
        }
    )

    command = [
        openclaw_executable,
        "gateway",
        "call",
        "chat.inject",
        "--params",
        params,
        "--json",
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("OpenClaw chat.inject timed out") from exc
    except OSError as exc:
        raise RuntimeError(
            f"Unable to run the OpenClaw CLI: {exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()

        details = stderr or stdout or "no diagnostic output"

        raise RuntimeError(
            f"OpenClaw chat.inject failed with exit code "
            f"{completed.returncode}: {details}"
        )

    try:
        response: Any = json.loads(completed.stdout)
    except json.JSONDecodeError:
        response = completed.stdout.strip()

    return (
        f"Message injected into OpenClaw session "
        f"{session_key}: {response}"
    )

def _build_snapshot_filename(camera_ip: str, event_type: str) -> str:
    """
    Shared naming scheme for every snapshot/marker file the event
    listener produces, so files can be found by camera, event type, and
    time without needing to open them:

        {camera_ip with dashes instead of dots}_{event_type}_{timestamp}.jpg

    e.g. "10-1-1-77_motion_true_20260718T215035.jpg"
    """
    safe_ip = camera_ip.replace(".", "-")
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{safe_ip}_{event_type}_{timestamp}.jpg"


def _fetch_motion_snapshot(camera: Camera, filename: str):
    """
    Download the given camera's current snapshot as a JPEG file, saved
    under the given filename. Returns the saved Path, or None on failure.
    """
    snapshot_uri = camera.profiles[0].snapshot_uri
    try:
        response = requests.get(
            snapshot_uri,
            auth=HTTPDigestAuth(os.environ.get("CAMERA_USERNAME", ""), os.environ.get("CAMERA_PASSWORD", "")),
            timeout=10,
        )
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to fetch motion snapshot: {e}")
        return None

    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / filename
    path.write_bytes(response.content)
    logger.debug(f"Saved motion snapshot to {path}")
    return path


def _save_empty_motion_marker(filename: str):
    """
    Save a 0-byte marker file recording a motion-ended (State: false)
    event without fetching a real image for it.
    """
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / filename
    path.touch()
    logger.debug(f"Recorded empty motion marker at {path}")
    return path


def _notify_openclaw_of_motion(camera_ip: str, snapshot_uri: str, filename: str) -> None:
    """
    POST to OpenClaw's /hooks/camera-motion endpoint (a named hook mapping
    configured in openclaw.json, NOT the generic /hooks/agent path),
    telling the agent exactly what to do and where to save its own copy
    of the snapshot and description - naming the exact tools and paths
    up front, rather than leaving the agent to rediscover a working
    sequence through trial and error on every motion event
    (get_snapshot_image_base64_encoded and the browser tool both proved
    unusable to it in earlier testing).

    Embeds the camera's snapshot_uri directly in the message rather than
    instructing OpenClaw to look it up via camera__get_camera - the
    caller already has it (from that camera's entry in
    _camera_subscriptions, populated once when its subscriptions were
    first resynced, since it doesn't change between events for a given
    camera). This removes one full tool round-trip (and the associated
    model reasoning step) from every motion notification.

    Requires an openclaw.json hooks.mappings entry like:

        {
          "id": "camera-motion",
          "match": { "path": "camera-motion" },
          "action": "agent",
          "wakeMode": "now",
          "name": "Camera Motion",
          "messageTemplate": "{{payload.message}}",
          "allowUnsafeExternalContent": true
        }

    allowUnsafeExternalContent must live in this mapping config, NOT in
    the JSON body we send: normalizeAgentPayload() (the generic
    /hooks/agent request parser) doesn't recognize that field at all, so
    sending it directly in our payload was silently dropped - confirmed
    by trajectory review showing the SECURITY NOTICE wrapper still
    present after we started sending it. Worse, /hooks/agent and
    /hooks/wake are special-cased in the request router and always
    return before hooks.mappings is ever consulted, so no mapping -
    including one matched by source rather than path - can apply to
    those two paths regardless of config. A mapped path is the only way
    to reach allowUnsafeExternalContent at all.

    Without it, OpenClaw wraps this message in a SECURITY NOTICE +
    EXTERNAL_UNTRUSTED_CONTENT boundary (since hook requests default to
    externalContentSource: "webhook"), telling the model not to follow
    instructions embedded in it - directly undermining the explicit
    numbered steps below. We control both the sender (this script) and
    the content (our own instructions), so this isn't actually untrusted
    third-party content; it's just labeled that way by default. Batch
    trajectory review showed the majority of sampled motion-event runs
    called get_snapshot_image_base64_encoded anyway - the exact tool
    step 1 explicitly says not to use - despite that instruction being
    present in every one of those runs, so this is a hypothesis to test
    against fresh, controlled events, not a confirmed fix.
    """
    file_path = f"$WORKSPACE_DIR/{OPENCLAW_SNAPSHOT_SUBDIR}/{filename}"
    description_path = f"$WORKSPACE_DIR/{OPENCLAW_SNAPSHOT_SUBDIR}/{Path(filename).stem}.txt"
    payload = {
        "message": (
            f"Motion detected on the camera at {camera_ip}. Do the following:\n"
            f"1. Call camera__download_snapshot_to_file with url set to exactly "
            f"\"{snapshot_uri}\" and file_path set to exactly \"{file_path}\".\n"
            f"2. Call read on that same path to view the image.\n"
            f"3. Write a brief description of what you see using the write "
            f"tool, saving it to exactly \"{description_path}\" as plain "
            f"text (just the description itself, no extra formatting).\n"
            f"4. Call camera__send_message_to_openclaw_chat with message set to "
            f"exactly that same description.\n"            
            "Do not call camera__get_camera or any other camera tool to look up "
            "the snapshot URL - it is already given above. Do not use "
            "get_snapshot_image_base64_encoded or the browser tool for this - "
            "go directly to download_snapshot_to_file, then read."
        ),
    }
    try:
        response = requests.post(
            OPENCLAW_HOOK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}"},
            timeout=10,
        )
        response.raise_for_status()
        logger.debug(f"Notified OpenClaw: {response.json()}")
    except Exception as e:
        logger.error(f"Failed to notify OpenClaw: {e}")


def _ensure_camera_subscription_entry(ip_address: str) -> dict:
    """
    Ensure the shared EventServer exists (starting it on the very first
    call across any camera) and this camera's own SubscriptionManager
    exists (creating it, and querying the camera fresh, the first time
    this particular camera is touched). Returns the _camera_subscriptions
    entry for this camera.
    """
    global _event_server

    if _event_server is None:
        _event_server = EventServer("0.0.0.0", EVENT_SERVER_PORT, _on_event_listener_event)
        _event_server.start()

    if ip_address not in _camera_subscriptions:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
        _camera_subscriptions[ip_address] = {
            "camera": camera,
            "subscription_manager": SubscriptionManager(camera),
        }

    return _camera_subscriptions[ip_address]


def _subscribe_camera_event_topic(ip_address: str, event_topic: str) -> None:
    """
    Subscribe a camera to a single ONVIF event topic, mirroring the real
    ONVIF Subscribe operation directly - it does not touch any of the
    camera's other active push subscriptions.
    """
    entry = _ensure_camera_subscription_entry(ip_address)
    camera = entry["camera"]
    subscription_manager = entry["subscription_manager"]

    subscription_manager.subscribe_push_event(camera, "0.0.0.0", EVENT_SERVER_PORT, event_topic)


def _unsubscribe_camera_events(ip_address: str) -> None:
    """
    Unsubscribe a camera from ALL of its ONVIF push subscriptions,
    mirroring the real ONVIF Unsubscribe operation directly - ONVIF has
    no operation to target a single topic while leaving others active,
    so this always clears everything for the camera at once.
    """
    entry = _ensure_camera_subscription_entry(ip_address)
    camera = entry["camera"]
    subscription_manager = entry["subscription_manager"]

    subscription_manager.unsubscribe_events(camera)


def _on_event_listener_event(alarms: list[dict]) -> None:
    """
    Callback invoked by EventServer's background thread on every incoming
    ONVIF event, from any camera - all cameras share this one EventServer,
    so this looks up which camera an event actually came from via its
    ip_address field (present on every parsed alarm) against
    _camera_subscriptions, rather than assuming a single fixed camera.

    Still only ACTS on VideoSource/MotionAlarm for now, even though a
    camera may genuinely be subscribed to other topics too (subscribing
    itself is already fully general via _subscribe_camera_event_topic
    above) - generalizing this handling logic to other topics is a
    separate, later step. Events on any other topic are received here
    but currently just ignored.

    State: "true" (real motion) saves a real local snapshot and notifies
    OpenClaw. State: "false" (motion ended) only records a 0-byte local
    marker file - no OpenClaw notification, since spending an agent run
    on "motion stopped" would reintroduce noise.
    """
    for alarm in alarms:
        logger.debug(f"Event listener event: {alarm}")

        camera_ip = alarm.get("ip_address")
        entry = _camera_subscriptions.get(camera_ip)
        if not entry:
            logger.error(f"Received event for camera at {camera_ip}, which has no tracked subscription; ignoring.")
            continue

        if alarm.get("topic") != "VideoSource/MotionAlarm":
            continue

        camera = entry["camera"]
        is_motion = str(alarm.get("data", {}).get("State", "")).lower() == "true"
        event_type = "motion_true" if is_motion else "motion_false"
        filename = _build_snapshot_filename(camera_ip, event_type)

        if is_motion:
            _fetch_motion_snapshot(camera, filename)
            _notify_openclaw_of_motion(camera_ip, camera.profiles[0].snapshot_uri, filename)
        else:
            _save_empty_motion_marker(filename)

def list_files(directory):
    """Recursively list all files in a directory."""
    for root, _, files in os.walk(directory):
        for file in files:
            yield os.path.join(root, file)

@mcp.tool()
def grep_search(pattern, directory, fileExtension=None):
    """Search for a regex pattern in files under a directory."""
    results = []

    # Validate directory
    if not os.path.isdir(directory):
        return {"error": f"Directory not found: {directory}"}

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}

    try:
        for file_path in list_files(directory):
            if fileExtension and not file_path.endswith(fileExtension):
                continue

            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, start=1):
                        if regex.search(line):
                            results.append({
                                "file": file_path,
                                "lineNum": line_num,
                                "line": line.strip()
                            })
            except (OSError, UnicodeDecodeError):
                # Skip unreadable files
                continue

    except Exception as e:
        return {"error": f"Search failed: {e}"}

    return {"matches": results}

@mcp.tool()
async def example_async_tool(context: Context) -> str:
    """
    Example async tool that asks the user a question via MCP elicitation,
    to experiment with the elicitation flow (server -> client -> user ->
    client -> server) as a building block for eventually responding to
    camera events interactively.
    """
    result = await context.elicit(
        message="What type of trip are you planning? Options: business, leisure, family, adventure",
        schema=TripTypeResponse,
    )
    if isinstance(result, AcceptedElicitation):
        return result.data.value
    elif isinstance(result, DeclinedElicitation):
        return "DECLINED"
    elif isinstance(result, CancelledElicitation):
        return "CANCELLED"
    return "INVALID RESPONSE"

@mcp.tool()
async def get_camera_mcp_version() -> str:
    """
    Get the version of the camera application, along with the version of the
    installed libonvif package it depends on.

    Returns:
        A JSON string with two fields:
            camera_mcp_version: version derived from the pyproject.toml file.
            libonvif_version: version of the installed libonvif package,
                               read via importlib.metadata.
    """

    camera_mcp_version = None
    current_file = Path(__file__)
    filename = Path(current_file.parent.parent) / "pyproject.toml"
    with open(filename, "r") as f:
        for line in f:
            if line.startswith("version"):
                camera_mcp_version = line.split("=")[1].strip().strip('"')
                logger.debug(f"Found camera_mcp version: {camera_mcp_version}")
                break

    try:
        libonvif_version = get_installed_version("libonvif")
    except Exception as e:
        logger.error(f"Failed to get libonvif version: {e}")
        libonvif_version = None

    return json.dumps({
        "camera_mcp_version": camera_mcp_version,
        "libonvif_version": libonvif_version,
    }, indent=4)

@mcp.tool()
async def set_camera_video_resolution(ip_address: str, profile_token: str, resolution: str) -> str:
    """
    Set the video resolution for one media profile on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, sets the resolution on that profile's video_encoder,
    then pushes the whole encoder configuration back to the camera in one
    ONVIF call. No JSON payload is needed - just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token whose video_encoder should
                       be pushed to the camera.
        resolution: A string in the exact format f"{width} x {height}"
                    (e.g. "1920 x 1080") - the same format used in the
                    camera JSON representation. Must be one of the
                    camera-reported valid resolutions for this profile's
                    current encoding.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                profile.video_encoder.resolution = resolution
                set_video_encoder_configuration(camera, profile.video_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set resolution to {resolution} for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set resolution for camera at {camera.xaddr}: {e}")
        return f"Failed to set resolution for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_video_frame_rate(ip_address: str, profile_token: str, frame_rate_limit: int) -> str:
    """
    Set the video frame rate limit for one media profile on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, sets the frame rate limit on that profile's
    video_encoder.rate_control, then pushes the whole encoder
    configuration back to the camera in one ONVIF call. No JSON payload
    is needed - just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token whose video_encoder should
                       be pushed to the camera.
        frame_rate_limit: Integer frames per second. Must fall within the
                    camera-reported valid frame rate range for this
                    profile's current encoding.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                profile.video_encoder.rate_control.frame_rate_limit = frame_rate_limit
                set_video_encoder_configuration(camera, profile.video_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set frame rate limit to {frame_rate_limit} for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set frame rate limit for camera at {camera.xaddr}: {e}")
        return f"Failed to set frame rate limit for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_video_bitrate(ip_address: str, profile_token: str, bitrate_limit: int) -> str:
    """
    Set the video bitrate limit for one media profile on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, sets the bitrate limit on that profile's
    video_encoder.rate_control, then pushes the whole encoder
    configuration back to the camera in one ONVIF call. No JSON payload
    is needed - just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token whose video_encoder should
                       be pushed to the camera.
        bitrate_limit: Integer kilobits per second. Must fall within the
                    camera-reported valid bitrate range for this
                    profile's current encoding.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                profile.video_encoder.rate_control.bitrate_limit = bitrate_limit
                set_video_encoder_configuration(camera, profile.video_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set bitrate limit to {bitrate_limit} for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set bitrate limit for camera at {camera.xaddr}: {e}")
        return f"Failed to set bitrate limit for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_video_gov_length(ip_address: str, profile_token: str, gov_length: int) -> str:
    """
    Set the video gov_length for one media profile on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, sets the gov_length on that profile's
    video_encoder, then pushes the whole encoder
    configuration back to the camera in one ONVIF call. No JSON payload
    is needed - just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token whose video_encoder should
                       be pushed to the camera.
        gov_length: Integer gov_length. Must fall within the
                    camera-reported valid gov length range for this
                    profile's current encoding.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                profile.video_encoder.gov_length = gov_length
                set_video_encoder_configuration(camera, profile.video_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set gov_length to {gov_length} for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set gov_length for camera at {camera.xaddr}: {e}")
        return f"Failed to set gov_length for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_audio_encoding(ip_address: str, profile_token: str, encoding: str) -> str:
    """
    Set the audio encoding for one media profile on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, sets the encoding on that profile's audio_encoder,
    then pushes the whole encoder configuration back to the camera in one
    ONVIF call. No JSON payload is needed - just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token whose audio_encoder should
                       be pushed to the camera.
        encoding: The codec name, e.g. "G711" or "AAC". Must match one of
                    the codecs the camera actually offers for this
                    profile.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                profile.audio_encoder.encoding = encoding
                set_audio_encoder_configuration(camera, profile.audio_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set audio encoding to {encoding} for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set audio encoding for camera at {camera.xaddr}: {e}")
        return f"Failed to set audio encoding for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_audio_sample_rate(ip_address: str, profile_token: str, sample_rate: int) -> str:
    """
    Set the audio sample rate for one media profile on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, sets the sample rate on that profile's audio_encoder,
    then pushes the whole encoder configuration back to the camera in one
    ONVIF call. No JSON payload is needed - just the camera's IP address.

    Note: on at least some hardware (observed on an Amcrest G711
    implementation), bitrate and sample_rate appear to be coupled -
    changing bitrate alone was silently ignored by the camera, while
    changing sample_rate caused bitrate to change along with it. Verify
    the result with a fresh get_cameras call afterward, since a "success"
    response does not guarantee the change was actually applied as
    requested.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token whose audio_encoder should
                       be pushed to the camera.
        sample_rate: Integer sample rate. Must be one of the values the
                    camera actually offers for this profile's current
                    encoding.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                profile.audio_encoder.sample_rate = sample_rate
                set_audio_encoder_configuration(camera, profile.audio_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set audio sample rate to {sample_rate} for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set audio sample rate for camera at {camera.xaddr}: {e}")
        return f"Failed to set audio sample rate for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def goto_camera_preset(camera_ptz_xaddr: str, camera_profile_token: str, camera_preset_token: str, camera_time_offset: int) -> str:
    """
    Move a PTZ camera to one of its stored presets.

    These four values come from the abbreviated per-camera summary
    produced by get_cameras (NOT the full camera representation):
      camera_ptz_xaddr    <- that camera's ptz_xaddr
      camera_time_offset  <- that camera's time_offset
      camera_profile_token <- almost always the main profile's token,
                              e.g. profiles[0].token, such as
                              "MediaProfile000"
      camera_preset_token <- the token of the desired entry in that
                             camera's ptz_presets list

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    This tool only sends the move command; it does not wait for the
    camera to finish moving or confirm it arrived. To check on that, call
    get_cameras again afterward and look at that camera's ptz_status
    field ("IDLE" once the move has completed).

    Args:
        camera_ptz_xaddr: That camera's ptz_xaddr, from get_cameras.
        camera_profile_token: The media profile token to command (almost
                       always the main profile, e.g. profiles[0].token).
        camera_preset_token: The token of the preset to move to, from
                       that camera's ptz_presets in get_cameras.
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds), from get_cameras.

    Returns:
        A message indicating success or failure
    """
    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    preset = PTZPreset(token=camera_preset_token)

    try:
        camera.errors = None
        goto_preset(camera, camera_profile_token, preset)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully moved camera at {camera_ptz_xaddr} to preset {camera_preset_token}."
    except Exception as e:
        logger.error(f"Failed to move camera at {camera_ptz_xaddr} to preset {camera_preset_token}: {e}")
        return f"Failed to move camera at {camera_ptz_xaddr} to preset {camera_preset_token}: {e}"

@mcp.tool()
async def set_camera_preset(ip_address: str, profile_token: str, preset_token: str = None, preset_name: str = None) -> str:
    """
    Create a new PTZ preset, or overwrite an existing one with the camera's
    current position.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, and edits it before pushing. No JSON payload is
    needed - just the camera's IP address.

    Two modes, based on whether preset_token is supplied:

    - preset_token omitted (create mode): the camera creates a brand new
      preset at its current position. Cameras support a limited number of
      presets - check how many already exist (e.g. via get_cameras'
      ptz_presets) before creating another, in case the camera silently
      rejects it once full. If preset_name is given, the new preset is
      created first, then renamed in a second call - the underlying ONVIF
      operation can't assign a name to a preset that doesn't have a token
      yet, so this tool creates it unnamed, determines the token the
      camera just assigned, then renames it. The camera doesn't move
      between these two calls, so the rename call safely re-saves the
      same position.

    - preset_token supplied (overwrite mode): the preset matching that
      token has its position overwritten to the camera's CURRENT
      position - not restored to wherever it used to point. If you only
      want to rename an existing preset without moving it, first call
      goto_camera_preset to move the camera back to that preset's own
      position, THEN call this tool - otherwise the preset's saved
      position will be silently replaced with wherever the camera
      happens to be sitting right now. Pass preset_name to also update
      the preset's stored name at the same time.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token to command (almost always
                       the main profile, e.g. profiles[0].token).
        preset_token: Token of an existing preset to overwrite (from
                      get_cameras' ptz_presets). Omit to create a new
                      preset instead.
        preset_name: Optional name to assign to the preset (new or
                     existing).

    Returns:
        A message indicating success or failure. On successful creation,
        includes the newly assigned preset token.
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None

        if preset_token:
            preset = None
            for candidate in (camera.ptz.presets if camera.ptz else []):
                if candidate.token == preset_token:
                    preset = candidate
                    break
            if not preset:
                return f"Preset {preset_token} not found on camera at {camera.xaddr}."
            if preset_name is not None:
                preset.name = preset_name
            set_preset(camera, profile_token, preset)
            if camera.errors:
                raise Exception(f"Camera returned errors: {camera.errors}")
            return f"Successfully overwrote preset {preset_token} on camera at {camera.xaddr} with its current position."

        # create mode
        existing_tokens = {p.token for p in (camera.ptz.presets if camera.ptz else [])}
        set_preset(camera, profile_token)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")

        get_presets(camera, profile_token)
        if camera.errors:
            raise Exception(f"Camera returned errors while refreshing presets: {camera.errors}")

        new_tokens = [p.token for p in camera.ptz.presets if p.token not in existing_tokens]
        if not new_tokens:
            return f"Preset created on camera at {camera.xaddr}, but could not determine its new token from the refreshed preset list."
        new_token = new_tokens[0]

        if preset_name is not None:
            new_preset = None
            for candidate in camera.ptz.presets:
                if candidate.token == new_token:
                    new_preset = candidate
                    break
            new_preset.name = preset_name
            set_preset(camera, profile_token, new_preset)
            if camera.errors:
                raise Exception(f"Preset {new_token} created, but failed to set its name: {camera.errors}")

        name_note = f" named '{preset_name}'" if preset_name else ""
        return f"Successfully created new preset {new_token}{name_note} on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set preset for camera at {camera.xaddr}: {e}")
        return f"Failed to set preset for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def remove_camera_preset(ip_address: str, profile_token: str, preset_token: str) -> str:
    """
    Permanently delete a PTZ preset from a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, and pushes the removal. No JSON payload is needed -
    just the camera's IP address.

    This removes the preset entirely - it is not the same as clearing or
    resetting a preset's position, and it cannot be undone from this
    tool. If you want to reuse a preset's token/slot for a different
    position instead of deleting it outright, use set_camera_preset in
    overwrite mode instead.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token to command (almost always
                       the main profile, e.g. profiles[0].token).
        preset_token: Token of the preset to remove (from get_cameras'
                      ptz_presets).

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    preset = None
    for candidate in (camera.ptz.presets if camera.ptz else []):
        if candidate.token == preset_token:
            preset = candidate
            break
    if not preset:
        return f"Preset {preset_token} not found on camera at {camera.xaddr}."

    try:
        camera.errors = None
        remove_preset(camera, profile_token, preset)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully removed preset {preset_token} from camera at {camera.xaddr}."
    except Exception as e:
        logger.error(f"Failed to remove preset {preset_token} from camera at {camera.xaddr}: {e}")
        return f"Failed to remove preset {preset_token} from camera at {camera.xaddr}: {e}"

@mcp.tool()
async def create_camera_preset_tour(ip_address: str, profile_token: str, tour_name: str = None) -> str:
    """
    Create a new, empty PTZ preset tour on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, and pushes the creation. No JSON payload is needed -
    just the camera's IP address.

    The underlying ONVIF CreatePresetTour operation has no name field, so
    if tour_name is given, this tool creates the tour first, determines
    the token the camera just assigned (by diffing the tour list
    before/after), then applies the name in a follow-up call - the tour
    has no spots yet either way, so this is a safe two-step sequence, the
    same pattern used by set_camera_preset for naming a newly-created
    preset.

    Once created, use set_camera_preset_tour to populate it with spots
    (preset token + stay time pairs), then start_camera_preset_tour to
    run it.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token to command (almost always
                       the main profile, e.g. profiles[0].token).
        tour_name: Optional name to assign to the new tour.

    Returns:
        A message indicating success or failure. On success, includes the
        newly assigned tour token.
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None
        existing_tokens = {t.token for t in (camera.ptz.tours if camera.ptz else [])}

        create_preset_tour(camera, profile_token)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")

        get_preset_tours(camera, profile_token)
        if camera.errors:
            raise Exception(f"Camera returned errors while refreshing tours: {camera.errors}")

        new_tokens = [t.token for t in camera.ptz.tours if t.token not in existing_tokens]
        if not new_tokens:
            return f"Tour created on camera at {camera.xaddr}, but could not determine its new token from the refreshed tour list."
        new_token = new_tokens[0]

        if tour_name is not None:
            new_tour = None
            for candidate in camera.ptz.tours:
                if candidate.token == new_token:
                    new_tour = candidate
                    break
            new_tour.name = tour_name
            modify_preset_tour(camera, profile_token, new_tour)
            if camera.errors:
                raise Exception(f"Tour {new_token} created, but failed to set its name: {camera.errors}")

        name_note = f" named '{tour_name}'" if tour_name else ""
        return f"Successfully created new preset tour {new_token}{name_note} on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to create preset tour for camera at {camera.xaddr}: {e}")
        return f"Failed to create preset tour for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_preset_tour(ip_address: str, profile_token: str, tour_token: str, tour_name: str = None, auto_start: bool = None, spots: list[dict] = None) -> str:
    """
    Update a PTZ preset tour's name, auto_start, and/or spots on a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, applies whichever of tour_name/auto_start/spots you
    supplied, then pushes the whole tour configuration in a single ONVIF
    call. Arguments left as None (the default) keep the tour's current
    value for that field - only supply the ones you actually want to
    change. No JSON payload is needed - just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token to command (almost always
                       the main profile, e.g. profiles[0].token).
        tour_token: The token of the tour to update (from get_cameras'
                    ptz_tours).
        tour_name: Optional new display name for the tour.
        auto_start: Optional new value for whether the tour starts
                    automatically under the camera's own configured
                    starting condition, rather than needing to be started
                    manually via start_camera_preset_tour.
        spots: Optional new list of stops for the tour, REPLACING its
               entire current spot list (not additive) - to add or remove
               a single spot, supply the full desired end-result list.
               Each entry is a dict with:
                 preset_token: must match a real preset (from
                               get_cameras' ptz_presets).
                 stay_time: an ISO 8601 duration string (e.g. "PT5S" for
                            5 seconds).
               e.g. [{"preset_token": "1", "stay_time": "PT5S"}, ...]

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    tour = None
    for candidate in (camera.ptz.tours if camera.ptz else []):
        if candidate.token == tour_token:
            tour = candidate
            break
    if not tour:
        return f"Tour {tour_token} not found on camera at {camera.xaddr}."

    if tour_name is not None:
        tour.name = tour_name
    if auto_start is not None:
        tour.auto_start = auto_start
    if spots is not None:
        tour.spots = [
            TourSpot(preset_token=spot.get("preset_token"), stay_time=spot.get("stay_time"))
            for spot in spots
        ]

    try:
        camera.errors = None
        modify_preset_tour(camera, profile_token, tour)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully updated preset tour {tour_token} on camera at {camera.xaddr}."
    except Exception as e:
        logger.error(f"Failed to update preset tour {tour_token} on camera at {camera.xaddr}: {e}")
        return f"Failed to update preset tour {tour_token} on camera at {camera.xaddr}: {e}"

@mcp.tool()
async def remove_camera_preset_tour(ip_address: str, profile_token: str, tour_token: str) -> str:
    """
    Permanently delete a PTZ preset tour from a camera.

    This function queries the camera directly via ONVIF using its IP
    address (with credentials from environment variables), builds a full
    Camera object, and pushes the removal. No JSON payload is needed -
    just the camera's IP address.

    This removes the tour entirely - it does not affect the individual
    presets used in its spots, only the tour itself - and cannot be
    undone from this tool.

    Args:
        ip_address: The IP address of the camera to command.
        profile_token: The media profile token to command (almost always
                       the main profile, e.g. profiles[0].token).
        tour_token: Token of the tour to remove (from get_cameras'
                    ptz_tours).

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    tour = None
    for candidate in (camera.ptz.tours if camera.ptz else []):
        if candidate.token == tour_token:
            tour = candidate
            break
    if not tour:
        return f"Tour {tour_token} not found on camera at {camera.xaddr}."

    try:
        camera.errors = None
        remove_preset_tour(camera, profile_token, tour)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully removed preset tour {tour_token} from camera at {camera.xaddr}."
    except Exception as e:
        logger.error(f"Failed to remove preset tour {tour_token} from camera at {camera.xaddr}: {e}")
        return f"Failed to remove preset tour {tour_token} from camera at {camera.xaddr}: {e}"

@mcp.tool()
async def start_camera_preset_tour(camera_ptz_xaddr: str, camera_profile_token: str, camera_ptz_tour_token: str, camera_time_offset: int) -> str:
    """
    Start running a PTZ preset tour on a camera.

    IMPORTANT: to stop this tour later, stop_camera_preset_tour needs the
    EXACT SAME camera_ptz_xaddr, camera_profile_token, and
    camera_ptz_tour_token used here. The success message from this call
    echoes all four argument values back to you in plain text - copy them
    directly from that message into the matching stop_camera_preset_tour
    call rather than trying to recall or reconstruct them later. None of
    these values are read from the camera or validated against anything;
    if any is wrong or missing when stopping, the camera will reject the
    request with an error like "Profile token does not exist", which is
    NOT a sign of a timing, authentication, or clock-sync problem - it
    means one of these argument values was wrong on that call.

    These four values come from the abbreviated per-camera summary
    produced by get_cameras (NOT the full camera representation):
      camera_ptz_xaddr    <- that camera's ptz_xaddr
      camera_time_offset  <- that camera's time_offset
      camera_profile_token <- almost always the main profile's token,
                              e.g. profiles[0].token, such as
                              "MediaProfile000"
      camera_ptz_tour_token <- the token of the desired entry in that
                               camera's ptz_tours list

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    The camera begins moving through the tour's spots in order, pausing
    at each for its configured stay_time, looping continuously until
    stop_camera_preset_tour is called. This does not wait for the tour to
    complete (it never does, on its own) or confirm it started - check
    that tour's status field via a fresh get_cameras call to see its
    reported state (e.g. "Idle" vs actively touring).

    Args:
        camera_ptz_xaddr: That camera's ptz_xaddr, from get_cameras.
        camera_profile_token: The media profile token to command (almost
                       always the main profile, e.g. profiles[0].token).
        camera_ptz_tour_token: Token of the tour to start, from that
                       camera's ptz_tours in get_cameras.
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds), from get_cameras.

    Returns:
        A message indicating success or failure. On success, echoes back
        all four argument values for you to reuse in the matching
        stop_camera_preset_tour call.
    """
    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    tour = PresetTour(token=camera_ptz_tour_token)

    try:
        camera.errors = None
        operate_preset_tour(camera, camera_profile_token, tour, "Start")
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return (
            f"Successfully started preset tour {camera_ptz_tour_token} on camera at {camera_ptz_xaddr}. "
            f"To stop it later, call stop_camera_preset_tour with these exact values: "
            f"camera_ptz_xaddr='{camera_ptz_xaddr}', camera_profile_token='{camera_profile_token}', "
            f"camera_ptz_tour_token='{camera_ptz_tour_token}', camera_time_offset={camera_time_offset}."
        )
    except Exception as e:
        logger.error(f"Failed to start preset tour {camera_ptz_tour_token} on camera at {camera_ptz_xaddr}: {e}")
        return f"Failed to start preset tour {camera_ptz_tour_token} on camera at {camera_ptz_xaddr}: {e}"

@mcp.tool()
async def stop_camera_preset_tour(camera_ptz_xaddr: str, camera_profile_token: str, camera_ptz_tour_token: str, camera_time_offset: int) -> str:
    """
    Stop a running PTZ preset tour on a camera.

    IMPORTANT: camera_ptz_xaddr, camera_profile_token, and
    camera_ptz_tour_token here must be the EXACT SAME values used in the
    start_camera_preset_tour call that started this tour - that call's
    success message echoed all four values back to you in plain text
    specifically so you could copy them directly into this call. None of
    these values are read from the camera or validated against anything -
    if any is wrong, missing, or reconstructed from memory incorrectly,
    the camera will reject this request with an error like "Profile
    token does not exist". That error means one of these argument values
    was wrong on THIS call - it is NOT a sign of a timing, authentication,
    or clock-sync problem, and re-syncing time or fetching a fresher
    camera JSON will not fix it. If you no longer have the exact values
    from when the tour was started, get camera_profile_token from that
    camera's profiles[0].token and camera_ptz_tour_token by matching the
    tour's name in a fresh get_cameras call.

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    Args:
        camera_ptz_xaddr: Must exactly match the value used in the
                       start_camera_preset_tour call for this tour.
        camera_profile_token: Must exactly match the value used in the
                       start_camera_preset_tour call for this tour.
        camera_ptz_tour_token: Must exactly match the value used in the
                       start_camera_preset_tour call for this tour.
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds) - a fresh value from get_cameras is
                       fine here even if it differs slightly from the
                       value used at start time.

    Returns:
        A message indicating success or failure
    """
    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    tour = PresetTour(token=camera_ptz_tour_token)

    try:
        camera.errors = None
        operate_preset_tour(camera, camera_profile_token, tour, "Stop")
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully stopped preset tour {camera_ptz_tour_token} on camera at {camera_ptz_xaddr}."
    except Exception as e:
        logger.error(f"Failed to stop preset tour {camera_ptz_tour_token} on camera at {camera_ptz_xaddr}: {e}")
        return f"Failed to stop preset tour {camera_ptz_tour_token} on camera at {camera_ptz_xaddr}: {e}"

@mcp.tool()
async def pan_tilt_camera(camera_ptz_xaddr: str, camera_profile_token: str, camera_time_offset: int, x: float, y: float) -> str:
    """
    Start a continuous pan/tilt move on a PTZ camera.

    These three values come from the abbreviated per-camera summary
    produced by get_cameras (NOT the full camera representation):
      camera_ptz_xaddr    <- that camera's ptz_xaddr
      camera_time_offset  <- that camera's time_offset
      camera_profile_token <- almost always the main profile's token,
                              e.g. profiles[0].token, such as
                              "MediaProfile000"

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    x and y are normalized velocities in the range -1.0 to 1.0 (0.0 means
    no motion on that axis): positive x pans right, negative x pans left;
    positive y tilts up, negative y tilts down. These are velocities, not
    positions - the camera keeps moving in that direction at that speed
    until stop_camera_pan_tilt is called.

    This does not stop on its own except at the camera's physical pan/tilt
    limits - most PTZ hardware halts at its mechanical range ends, so
    forgetting to stop is not unsafe, but the camera will simply drift to
    whichever limit it's heading toward and park there rather than stopping
    at a precise point. Call stop_camera_pan_tilt to halt motion exactly
    where you want it, or check that camera's ptz_status field via a fresh
    get_cameras call to see where it ended up.

    This is pan/tilt only - it has no effect on zoom. Use zoom_camera
    separately for zoom; a camera can only perform one of pan/tilt or zoom
    at a time.

    Args:
        camera_ptz_xaddr: That camera's ptz_xaddr, from get_cameras.
        camera_profile_token: The media profile token to command (almost
                       always the main profile, e.g. profiles[0].token).
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds), from get_cameras.
        x: Pan velocity, -1.0 (left) to 1.0 (right). 0.0 for no pan.
        y: Tilt velocity, -1.0 (down) to 1.0 (up). 0.0 for no tilt.

    Returns:
        A message indicating success or failure
    """
    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    try:
        camera.errors = None
        continuous_move(camera, camera_profile_token, x, y, 0)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully started pan/tilt move on camera at {camera_ptz_xaddr} (x={x}, y={y})."
    except Exception as e:
        logger.error(f"Failed to start pan/tilt move on camera at {camera_ptz_xaddr}: {e}")
        return f"Failed to start pan/tilt move on camera at {camera_ptz_xaddr}: {e}"

@mcp.tool()
async def zoom_camera(camera_ptz_xaddr: str, camera_profile_token: str, camera_time_offset: int, z: float) -> str:
    """
    Start a continuous zoom move on a PTZ camera.

    These three values come from the abbreviated per-camera summary
    produced by get_cameras (NOT the full camera representation):
      camera_ptz_xaddr    <- that camera's ptz_xaddr
      camera_time_offset  <- that camera's time_offset
      camera_profile_token <- almost always the main profile's token,
                              e.g. profiles[0].token, such as
                              "MediaProfile000"

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    z is a normalized velocity in the range -1.0 to 1.0, excluding 0.0:
    positive zooms in (telephoto), negative zooms out (wide). This is a
    velocity, not a position - the camera keeps zooming at that speed
    until stop_camera_zoom is called. z=0.0 is rejected here rather than
    silently doing nothing; use stop_camera_zoom if you want to halt an
    in-progress zoom.

    This does not stop on its own except at the camera's physical zoom
    limits (fully wide or fully telephoto) - most PTZ hardware halts
    there, so forgetting to stop is not unsafe, but the camera will simply
    zoom to whichever limit it's heading toward and stop there rather than
    at a precise point. Call stop_camera_zoom to halt zoom exactly where
    you want it, or check that camera's ptz_status field via a fresh
    get_cameras call to see where it ended up.

    This is zoom only - it has no effect on pan/tilt. Use pan_tilt_camera
    separately for pan/tilt; a camera can only perform one of pan/tilt or
    zoom at a time.

    Args:
        camera_ptz_xaddr: That camera's ptz_xaddr, from get_cameras.
        camera_profile_token: The media profile token to command (almost
                       always the main profile, e.g. profiles[0].token).
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds), from get_cameras.
        z: Zoom velocity, -1.0 (zoom out) to 1.0 (zoom in). Must not be 0.0.

    Returns:
        A message indicating success or failure
    """
    if z == 0:
        return "z must not be 0.0 - to stop an in-progress zoom, call stop_camera_zoom instead."

    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    try:
        camera.errors = None
        continuous_move(camera, camera_profile_token, 0, 0, z)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully started zoom move on camera at {camera_ptz_xaddr} (z={z})."
    except Exception as e:
        logger.error(f"Failed to start zoom move on camera at {camera_ptz_xaddr}: {e}")
        return f"Failed to start zoom move on camera at {camera_ptz_xaddr}: {e}"

@mcp.tool()
async def stop_camera_pan_tilt(camera_ptz_xaddr: str, camera_profile_token: str, camera_time_offset: int) -> str:
    """
    Stop an in-progress continuous pan/tilt move started by pan_tilt_camera.

    These three values must match what was used in the pan_tilt_camera
    call that started the move - they come from the abbreviated
    per-camera summary produced by get_cameras (NOT the full camera
    representation):
      camera_ptz_xaddr    <- that camera's ptz_xaddr
      camera_time_offset  <- that camera's time_offset
      camera_profile_token <- almost always the main profile's token,
                              e.g. profiles[0].token, such as
                              "MediaProfile000"

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    Has no effect on zoom - use stop_camera_zoom to stop a zoom move. If no
    pan/tilt move is currently in progress, this is a harmless no-op on
    most cameras.

    Args:
        camera_ptz_xaddr: That camera's ptz_xaddr, from get_cameras.
        camera_profile_token: The media profile token to command (should
                       match whatever was used in the pan_tilt_camera call).
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds) - a fresh value from get_cameras is
                       fine here even if it differs slightly from the
                       value used when the move was started.

    Returns:
        A message indicating success or failure
    """
    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    try:
        camera.errors = None
        move_stop(camera, camera_profile_token, is_zoom=False)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully stopped pan/tilt move on camera at {camera_ptz_xaddr}."
    except Exception as e:
        logger.error(f"Failed to stop pan/tilt move on camera at {camera_ptz_xaddr}: {e}")
        return f"Failed to stop pan/tilt move on camera at {camera_ptz_xaddr}: {e}"

@mcp.tool()
async def stop_camera_zoom(camera_ptz_xaddr: str, camera_profile_token: str, camera_time_offset: int) -> str:
    """
    Stop an in-progress continuous zoom move started by zoom_camera.

    These three values must match what was used in the zoom_camera call
    that started the move - they come from the abbreviated per-camera
    summary produced by get_cameras (NOT the full camera representation):
      camera_ptz_xaddr    <- that camera's ptz_xaddr
      camera_time_offset  <- that camera's time_offset
      camera_profile_token <- almost always the main profile's token,
                              e.g. profiles[0].token, such as
                              "MediaProfile000"

    Credentials come from the CAMERA_USERNAME/CAMERA_PASSWORD environment
    variables, not from any of these arguments.

    Has no effect on pan/tilt - use stop_camera_pan_tilt to stop a pan/tilt
    move. If no zoom move is currently in progress, this is a harmless
    no-op on most cameras.

    Args:
        camera_ptz_xaddr: That camera's ptz_xaddr, from get_cameras.
        camera_profile_token: The media profile token to command (should
                       match whatever was used in the zoom_camera call).
        camera_time_offset: That camera's time_offset (an integer number
                       of seconds) - a fresh value from get_cameras is
                       fine here even if it differs slightly from the
                       value used when the move was started.

    Returns:
        A message indicating success or failure
    """
    if not camera_ptz_xaddr:
        return (
            "camera_ptz_xaddr is required - call get_cameras again to get "
            "an up to date summary before retrying."
        )

    camera = Camera()
    camera.capabilities = Capabilities(ptz=PTZCapabilities(xaddr=camera_ptz_xaddr))
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")
    camera.time_offset = camera_time_offset

    try:
        camera.errors = None
        move_stop(camera, camera_profile_token, is_zoom=True)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully stopped zoom move on camera at {camera_ptz_xaddr}."
    except Exception as e:
        logger.error(f"Failed to stop zoom move on camera at {camera_ptz_xaddr}: {e}")
        return f"Failed to stop zoom move on camera at {camera_ptz_xaddr}: {e}"

@mcp.tool()
async def change_camera_hostname(ip_address: str, new_hostname: str) -> str:
    """
    Change the hostname of a camera by IP address.

    This function queries the camera directly via ONVIF using its IP address
    (with credentials from environment variables), builds a full Camera object,
    and pushes the new hostname. No JSON string payload is needed — just the
    camera's IP and the desired hostname.

    Args:
        ip_address: The IP address of the camera to re-name.
        new_hostname: The new hostname to set.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.hostname.name = new_hostname
        camera.errors = None
        set_hostname(camera)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully changed hostname of camera at {camera.xaddr} to {new_hostname}."
    except Exception as e:
        logger.error(f"Failed to change hostname for camera at {camera.xaddr}: {e}")
        return f"Failed to change hostname for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def sync_camera_time(ip_address: str) -> str:
    """
    Synchronize a camera's clock to this machine's current local time.

    Useful for correcting a camera whose internal clock has drifted or
    reset (e.g. after a power loss reverting it to an epoch default like
    2000-01-01), which otherwise produces confusing timestamps on
    snapshots and event data.

    This function queries the camera directly via ONVIF using its IP address
    (with credentials from environment variables), builds a full Camera object,
    synchronizes its clock, then re-queries to report the resulting time offset.
    No JSON string payload is needed — just the camera's IP address.

    Args:
        ip_address: The IP address of the camera to sync.

    Returns:
        A message indicating success or failure, including the resulting
        time_offset in seconds if successful.

    Note: The returned time_offset value is used during ONVIF authentication.
    Callers should save this new time_offset into their local camera JSON
    summary (as returned by get_cameras) after a sync completes successfully,
    otherwise future ONVIF calls to this camera may fail with timestamp-based
    auth errors. The get_cameras tool reads time_offset from each discovered
    camera at discovery time, so the saved value will be used for the next
    sync_camera_time or other IP-address tool calls.
    """
    try:
        camera = get_camera_by_ip(
            ip_address,
            os.environ.get("CAMERA_USERNAME", ""),
            os.environ.get("CAMERA_PASSWORD", ""),
        )
    except Exception as e:
        logger.error(f"Failed to query camera at {ip_address}: {e}")
        return f"Failed to query camera at {ip_address}: {e}"

    try:
        camera.errors = None
        sdt = get_local_date_and_time()
        set_system_date_and_time(camera, sdt)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        get_time_offset(camera)
        return f"Successfully synchronized time for camera at {camera.xaddr}. time_offset is now {camera.time_offset} seconds."
    except Exception as e:
        logger.error(f"Failed to sync time for camera at {camera.xaddr}: {e}")
        return f"Failed to sync time for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def check_camera_mcp_environment() -> str:
    """
    Collect information about the environment under which camera server is running
    
    Args:
        None

    Returns:
        A delimited string containing environment variable settings

    """

    output = []
    output.append(os.environ.get("CAMERA_USERNAME", "Empty $env:CAMERA_USERNAME"))
    output.append(os.environ.get("CAMERA_PASSWORD", "Empty $env:CAMERA_PASSWORD"))
    output.append(os.environ.get("STREAM_SERVER_IP", "Empty $env:STREAM_SERVER_IP"))
    output.append(os.environ.get("PATH", "Empty $env:PATH"))

    return "\n--\n".join(output)

@mcp.tool()
async def stream_camera(camera_device_information_serial_number: str, camera_media_profile_token: str) -> str:
    """
    Open a camera live stream in the user's default web browser.

    Args:
        camera_device_information_serial_number: The camera serial number found in the ONVIF data of the camera
                                                 that is stored in the device_information topic group.

        camera_media_profile_token: The media profile token found the ONVIF data topic profiles. The default choice
                                    should be the first profile.

    Returns:
        A message indicating success or failure
    """
    #http://10.1.1.76:8889/AMC014641NE6L35AT8/MediaProfile000
    url = f"http://{os.environ.get("STREAM_SERVER_IP")}:8889/{camera_device_information_serial_number}/{camera_media_profile_token}"
    opened = webbrowser.open(url)
    if opened:
        return f"Opened {url} in default browser."
    else:
        return f"Failed to open {url}."
    
@mcp.tool()
async def get_snapshot_image_base64_encoded(url: str) -> str:
    """
    Get a snapshot image from a camera as a base64-encoded string.

    Args:
        url: The full URL to the snapshot, e.g. "https://example.com/snapshot.jpg"

    Returns:
        The snapshot image as a base64-encoded string.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"Refused to get snapshot from '{url}': must start with http:// or https://")

    try:
        response = requests.get(url, auth=HTTPDigestAuth(os.environ.get("CAMERA_USERNAME", ""), os.environ.get("CAMERA_PASSWORD", "")), timeout=5)
        response.raise_for_status()
        return base64.b64encode(response.content).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to get snapshot from {url}: {e}")
        return None

@mcp.tool()
async def download_snapshot_to_file(url: str, file_path: str) -> str:
    """
    Download a snapshot from a camera to a specified file path.

    Args:
        url: The full URL to the snapshot, e.g. "https://example.com/snapshot.jpg"
        file_path: The local file path where the snapshot will be saved.

    Returns:
        A message indicating success or failure.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Refused to download '{url}': must start with http:// or https://"

    try:
        response = requests.get(url, auth=HTTPDigestAuth(os.environ.get("CAMERA_USERNAME", ""), os.environ.get("CAMERA_PASSWORD", "")), timeout=5)
        response.raise_for_status()
        with open(file_path, 'wb') as f:
            f.write(response.content)
        return f"Snapshot downloaded successfully to {file_path}."
    except Exception as e:
        logger.error(f"Failed to download snapshot from {url}: {e}")
        return f"Failed to download snapshot from {url}: {e}"

@mcp.tool()
async def show_snapshot_in_browser(url: str) -> str:
    """
    Open a snapshot URL in the user's default web browser.

    Args:
        url: The full URL to open, e.g. "https://example.com"

    Returns:
        A confirmation message.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Refused to open '{url}': must start with http:// or https://"

    curl = f"{url[:7]}{os.environ.get("CAMERA_USERNAME", "")}:{os.environ.get("CAMERA_PASSWORD", "")}@{url[7:]}"
    opened = webbrowser.open(curl)
    if opened:
        return f"Opened {url} in default browser."
    else:
        return f"Failed to open {url}."
    
@mcp.tool()
async def get_camera(ip_address: str) -> str:
    """
    Get information about a camera at the specified IP address.

    Args:
        ip_address: The IP address of the camera to retrieve.

    Returns:
        A string representation of the camera's information.
    """

    camera = get_camera_by_ip(ip_address, os.environ.get("CAMERA_USERNAME", ""), os.environ.get("CAMERA_PASSWORD", ""))
    return camera.to_json()

@mcp.tool()
async def update_camera_data(json_string: str) -> str:
    """
    Re-query a camera fresh, using the xaddr and credentials currently set
    in the given camera JSON.

    Use this after editing username or password in the JSON returned by
    get_camera/get_cameras - for example, to try different credentials
    against a camera that failed authorization the first time. The edited
    credentials are what get used for the fresh query, not whatever was
    originally used. Any other edits made elsewhere in the JSON are
    ignored, since this re-runs the full query from scratch rather than
    patching the existing data - the returned camera reflects the device's
    actual current state, not your edits (aside from username/password,
    which control how the query is authorized).

    Do not edit xaddr. It is the camera's own self-reported device service
    address, discovered without authorization, and functions as the
    camera's network identity rather than a configurable setting. Changing
    it points this tool at a different device entirely rather than
    re-querying the same camera.

    Args:
        json_string: The JSON string representation of the camera, as
                     returned by get_camera or get_cameras, with the
                     desired username/password already edited.

    Returns:
        The freshly queried camera as a JSON string, or an error message
        if the JSON could not be parsed or the query itself failed (e.g.
        the credentials are still not authorized).
    """
    try:
        camera = camera_from_json(json_string)
    except Exception as e:
        logger.error(f"Failed to parse camera JSON: {e}")
        return f"Failed to parse camera JSON: {e}"

    try:
        refreshed = refresh_camera(camera)
        return refreshed.to_json()
    except Exception as e:
        logger.error(f"Failed to refresh camera at {camera.xaddr}: {e}")
        return f"Failed to refresh camera at {camera.xaddr}: {e}"

@mcp.tool()
async def get_cameras() -> str:
    """
    Discover cameras on the local network and return lightweight summaries.

    Each summary contains only the fields an agent typically needs to reason
    about — hostname, device info, profile tokens, encoder config (from the
    first/primary profile), PTZ presets, tours, snapshot & stream URIs, and
    time offset. All the noisy ONVIF boilerplate (codec resolution lists,
    multicast settings, SOAP addresses, network interface details, imaging
    options, etc.) is stripped away.

    Returns:
        A delimited string containing a summary dict for each camera found on
        the local network. Each camera's summary is separated by "\n--\n".
    """

    ip_address = "0.0.0.0"
    if sys.platform == "win32":
        ips = find_adapters()
        if len(ips):
            ip_address = ips[0]
            logger.debug(f"host ip addresses: {ips}")

    cameras = discover(ip_address,
                       get_camera_credentials,
                       on_error=on_error,
                       camera_filled=camera_filled,
                       use_threads=True)

    logger.debug(f"Discovered {len(cameras)} camera(s)")

    summaries = []
    for camera in cameras:
        # Serialize once via the same codec used by Camera.to_json(), then
        # project down to just the fields this summary needs. Every field
        # below is read with `.get(key) or default` rather than
        # `.get(key, default)`, since to_dict() always includes every
        # dataclass field explicitly (even when its value is None) - the
        # dict.get default only kicks in for a missing key, not a present
        # key holding None, so relying on it here would silently produce
        # None instead of the intended fallback.
        try:
            data = to_dict(camera)
        except Exception as e:
            logger.error(f"Failed to serialize camera at {getattr(camera, 'xaddr', '?')}: {e}")
            continue

        dev = data.get("device_information") or {}
        hostname_obj = data.get("hostname") or {}
        xaddr = data.get("xaddr") or ""
        ip_addr = xaddr.split("://", 1)[1].split("/", 1)[0] if "://" in xaddr else ""

        profiles = []
        for p in data.get("profiles") or []:
            video_encoder = p.get("video_encoder") or {}
            rate_control = video_encoder.get("rate_control") or {}
            audio_encoder = p.get("audio_encoder") or {}
            profiles.append({
                "token": p.get("token") or "",
                "name": p.get("name") or "",
                "video_encoder": {
                    "encoding": video_encoder.get("encoding") or "",
                    "resolution": video_encoder.get("resolution") or "",
                    "frame_rate_limit": rate_control.get("frame_rate_limit") or 0,
                    "bitrate_limit": rate_control.get("bitrate_limit") or 0,
                    "gov_length": video_encoder.get("gov_length") or 0
                },
                "audio_encoder": {
                    "encoding": audio_encoder.get("encoding") or "",
                    "sample_rate": audio_encoder.get("sample_rate") or 0
                },
                "stream_uri": p.get("stream_uri") or "",
                "snapshot_uri": p.get("snapshot_uri") or ""
            })

        ptz = data.get("ptz") or {}

        presets = [
            {"token": pr.get("token") or "", "name": pr.get("name") or ""}
            for pr in ptz.get("presets") or []
        ]

        tours = []
        for t in ptz.get("tours") or []:
            tour_status = t.get("status") or {}
            tours.append({
                "token": t.get("token") or "",
                "name": t.get("name") or "",
                "status": tour_status.get("state") or "",
                "spot_count": len(t.get("spots") or [])
            })

        ptz_status = ptz.get("status") or {}
        ptz_st = {
            "pan_tilt": ptz_status.get("pan_tilt_status") or "",
            "zoom": ptz_status.get("zoom_status") or ""
        }

        caps = data.get("capabilities") or {}
        ptz_caps = caps.get("ptz") or {}
        ptz_xaddr = ptz_caps.get("xaddr") or ""

        event_props = data.get("event_properties") or {}
        event_topics = event_props.get("topic_set") or []

        summary = {
            "hostname": hostname_obj.get("name") or data.get("name") or "",
            "ip_address": ip_addr,
            "manufacturer": dev.get("manufacturer") or "",
            "model": dev.get("model") or "",
            "firmware_version": dev.get("firmware_version") or "",
            "serial_number": dev.get("serial_number") or "",
            "profiles": profiles,
            "ptz_presets": presets,
            "ptz_tours": tours,
            "ptz_status": ptz_st,
            "ptz_xaddr": ptz_xaddr,
            "event_topics": event_topics,
            "subscribed_events": list(_subscribed_events_by_camera.get(ip_addr, [])),
            "time_offset": int(data.get("time_offset") or 0)
        }
        summaries.append(json.dumps(summary))

    return "\n--\n".join(summaries)

@mcp.tool()
async def add_subscribed_event(ip_address: str, event_topic: str) -> str:
    """
    Subscribe a camera to an ONVIF event topic and mark it as observed.

    Updates this server's own bookkeeping (visible afterward as that
    camera's subscribed_events list in get_cameras) AND performs the
    real ONVIF subscription on the camera itself - adding just this one
    topic, without touching any of the camera's other active
    subscriptions (this mirrors ONVIF's own Subscribe operation, which
    is likewise additive/per-topic). All cameras share one underlying
    event listener - the first call to this tool or
    unsubscribe_all_events, for any camera, starts it; every subsequent
    call (for that camera or any other) reuses it.

    If the real subscription fails (e.g. the camera is unreachable), the
    bookkeeping change is rolled back rather than left showing a topic
    as subscribed when it isn't.

    event_topic is not validated against the camera's real topics here -
    it should be one of the strings in that camera's event_topics list
    (from get_cameras), but a typo will be sent to the camera as a
    literal (and likely rejected or silently non-matching) topic filter.

    Args:
        ip_address: The IP address of the camera.
        event_topic: The event topic string to add, e.g.
                     "RuleEngine/CellMotionDetector/Motion" - see that
                     camera's event_topics list in get_cameras for the
                     full set of valid values.

    Returns:
        A message indicating the result, including the resulting list.
    """
    events = _subscribed_events_by_camera.setdefault(ip_address, [])
    if event_topic in events:
        return f"{event_topic} is already in the subscribed_events list for camera at {ip_address}. Current list: {events}"

    events.append(event_topic)

    try:
        _subscribe_camera_event_topic(ip_address, event_topic)
        return f"Added {event_topic} to the subscribed_events list for camera at {ip_address}, and subscribed on the camera. Current list: {events}"
    except Exception as e:
        events.remove(event_topic)
        logger.error(f"Failed to subscribe camera at {ip_address} to {event_topic}: {e}")
        return f"Failed to subscribe camera at {ip_address} to {event_topic}: {e}"

@mcp.tool()
async def unsubscribe_all_events(ip_address: str) -> str:
    """
    Unsubscribe a camera from ALL of its ONVIF event topics and clear it
    from observation.

    Updates this server's own bookkeeping (visible afterward as that
    camera's subscribed_events list in get_cameras) AND performs the
    real ONVIF unsubscription on the camera itself. This mirrors ONVIF's
    own Unsubscribe operation directly: it has no way to target a single
    subscription while leaving others active, so it always removes every
    push subscription for the camera at once. To resume observing any
    topics afterward, call add_subscribed_event again for each one.

    If the real unsubscription fails (e.g. the camera is unreachable),
    the bookkeeping change is rolled back rather than left showing an
    empty subscribed_events list when the camera might still be sending
    events.

    Args:
        ip_address: The IP address of the camera.

    Returns:
        A message indicating the result, including the resulting list.
    """
    events = _subscribed_events_by_camera.get(ip_address, [])
    if not events:
        return f"Camera at {ip_address} has no subscribed events. Current list: {events}"

    previous_events = list(events)
    events.clear()

    try:
        _unsubscribe_camera_events(ip_address)
        return f"Unsubscribed camera at {ip_address} from all events. Current list: {events}"
    except Exception as e:
        events.extend(previous_events)
        logger.error(f"Failed to unsubscribe camera at {ip_address} from all events: {e}")
        return f"Failed to unsubscribe camera at {ip_address} from all events: {e}"


def main():
    logger.debug("Server starting...")
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()