import base64
import json
import logging
from importlib.metadata import version as get_installed_version
from pathlib import Path
from libonvif.utils.adapters import find_adapters
from libonvif.devices.camera import Camera, discover, get_camera_by_ip, set_hostname, \
        set_video_encoder_configuration, set_audio_encoder_configuration, camera_from_json, refresh_camera, \
        goto_preset
from mcp.server.fastmcp import FastMCP
import os
import sys
import webbrowser
import niquests as requests
from niquests.auth import HTTPDigestAuth
import re


logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
logger = logging.getLogger(__name__)

mcp = FastMCP("camera")

USER_AGENT = "camera-app/1.0"

def get_camera_credentials(camera: Camera) -> None:
    camera.username = os.environ.get("CAMERA_USERNAME", "")
    camera.password = os.environ.get("CAMERA_PASSWORD", "")

def on_error(xaddr: str, ex: Exception) -> None:
    logger.debug(f"error: {xaddr} - {ex}")

def camera_filled(camera: Camera) -> None:
    logger.debug(f"Camera Filled: {camera.hostname} : {camera.device_information.serial_number}")

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
async def set_camera_video_encoder(json_string: str, profile_token: str) -> str:
    """
    Push the video_encoder configuration for one media profile to a camera.

    Unlike a function with individual arguments per setting, this tool works
    directly on the camera's JSON representation (as returned by get_camera
    or get_cameras): edit whichever fields you want to change inside
    profiles[profile_token].video_encoder in that JSON, then pass the
    edited JSON string back in here. Every field currently set under that
    profile's video_encoder is pushed to the camera in a single ONVIF call -
    there is no need for a separate tool per field.

    Only the video_encoder node of the matching profile is read and pushed.
    Edits made anywhere else in the JSON (device_information, hostname,
    network_interfaces, other profiles, etc.) are ignored by this tool.

    Editable fields under profiles[profile_token].video_encoder, and how to
    choose a valid value for each:

        encoding
            The codec name, e.g. "H264". Must be one of the codecs the
            camera actually offers - check which of jpeg / mpeg4 / h264 are
            non-null under this same profile's video_encoder_options.

        resolution
            A string in the exact format f"{width} x {height}" (e.g.
            "1920 x 1080"). The (width, height) pair must be one of the
            entries in video_encoder_options.<codec>.resolutions_available
            for this profile's encoding (e.g. video_encoder_options.h264.
            resolutions_available when encoding is "H264"). Do not invent a
            resolution not present in that list.

        rate_control.frame_rate_limit
            Integer frames per second. Must fall within
            video_encoder_options.<codec>.frame_rate_range (min/max).

        multicast.ip_address
            A multicast IPv4 address (224.0.0.0-239.255.255.255). Leave as
            the camera's existing value unless you specifically need to
            change the multicast group.

        multicast.port
            Integer UDP port for the multicast stream.

        multicast.ttl
            Integer time-to-live (hop count) for multicast packets.

        session_timeout
            An ISO 8601 duration string, e.g. "PT60S" for 60 seconds.

        gov_length
            Integer GOP length (frames between keyframes). Must fall within
            video_encoder_options.<codec>.gov_length_range (min/max).

        profile
            The H.264 profile name, e.g. "Baseline", "Main", or "High".
            Must be one of the entries in
            video_encoder_options.h264.profiles_supported.

    Args:
        json_string: The JSON string representation of the camera, as
                     returned by get_camera or get_cameras, with the desired
                     changes already made under
                     profiles[profile_token].video_encoder.
        profile_token: The media profile token whose video_encoder should be
                       pushed to the camera.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = camera_from_json(json_string)
    except Exception as e:
        logger.error(f"Failed to parse camera JSON: {e}")
        return f"Failed to parse camera JSON: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                set_video_encoder_configuration(camera, profile.video_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set video encoder configuration for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set video encoder configuration for camera at {camera.xaddr}: {e}")
        return f"Failed to set video encoder configuration for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def set_camera_audio_encoder(json_string: str, profile_token: str) -> str:
    """
    Push the audio_encoder configuration for one media profile to a camera.

    Like set_camera_video_encoder, this tool works directly on the camera's
    JSON representation (as returned by get_camera or get_cameras): edit
    whichever fields you want to change inside
    profiles[profile_token].audio_encoder in that JSON, then pass the
    edited JSON string back in here. Every field currently set under that
    profile's audio_encoder is pushed to the camera in a single ONVIF call.

    Only the audio_encoder node of the matching profile is read and pushed.
    Edits made anywhere else in the JSON (device_information, hostname,
    video_encoder, other profiles, etc.) are ignored by this tool.

    Editable fields under profiles[profile_token].audio_encoder, and how to
    choose a valid value for each:

        encoding
            The codec name, e.g. "G711" or "AAC". Must match one of the
            entries in this same profile's audio_encoder_options list (each
            entry there has its own .encoding).

        bitrate
            Integer bitrate. Must be one of the values in
            audio_encoder_options[i].bitrate_list for the entry whose
            .encoding matches this encoder's encoding.

            Note: on at least some hardware (observed on an Amcrest G711
            implementation), bitrate and sample_rate appear to be coupled -
            changing bitrate alone was silently ignored by the camera, while
            changing sample_rate caused bitrate to change along with it.
            If you need a specific bitrate, try setting sample_rate to the
            value that pairs with it and verify both fields with a fresh
            get_camera call afterward, since a "success" response does not
            guarantee every field you set was actually applied.

        sample_rate
            Integer sample rate. Must be one of the values in
            audio_encoder_options[i].sample_rate_list for the entry whose
            .encoding matches this encoder's encoding.

            See the note under bitrate above - on some hardware this is the
            field that actually drives the change, with bitrate following
            it rather than being independently settable.

        multicast.ip_address
            A multicast IPv4 address (224.0.0.0-239.255.255.255). Leave as
            the camera's existing value unless you specifically need to
            change the multicast group.

        multicast.port
            Integer UDP port for the multicast stream.

        multicast.ttl
            Integer time-to-live (hop count) for multicast packets.

        session_timeout
            An ISO 8601 duration string, e.g. "PT30S" for 30 seconds.

    Args:
        json_string: The JSON string representation of the camera, as
                     returned by get_camera or get_cameras, with the desired
                     changes already made under
                     profiles[profile_token].audio_encoder.
        profile_token: The media profile token whose audio_encoder should be
                       pushed to the camera.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = camera_from_json(json_string)
    except Exception as e:
        logger.error(f"Failed to parse camera JSON: {e}")
        return f"Failed to parse camera JSON: {e}"

    try:
        camera.errors = None

        for profile in camera.profiles:
            if profile.token == profile_token:
                set_audio_encoder_configuration(camera, profile.audio_encoder)
                if camera.errors:
                    raise Exception(f"Camera returned errors: {camera.errors}")
                return f"Successfully set audio encoder configuration for camera at {camera.xaddr}, profile {profile_token}."

        return f"Profile {profile_token} not found on camera at {camera.xaddr}."

    except Exception as e:
        logger.error(f"Failed to set audio encoder configuration for camera at {camera.xaddr}: {e}")
        return f"Failed to set audio encoder configuration for camera at {camera.xaddr}: {e}"

@mcp.tool()
async def goto_camera_preset(json_string: str, profile_token: str, preset_token: str) -> str:
    """
    Move a PTZ camera to one of its stored presets.

    Presets are found at camera.ptz.presets in the camera's JSON
    representation (as returned by get_camera/get_cameras) - a list of
    PTZPreset entries, each with a token and a (often blank) name. Find
    the preset you want by matching its token or name in that list, then
    pass its token here as preset_token.

    profile_token should almost always be the camera's main media profile
    token - typically profiles[0].token, e.g. "MediaProfile000" - since
    PTZ presets are defined per-profile and the main profile is where
    they're normally stored.

    This tool only sends the move command; it does not wait for the
    camera to finish moving or confirm it arrived. To check on that,
    call get_camera again afterward and look at ptz.status.pan_tilt_status
    and ptz.status.zoom_status ("IDLE" once the move has completed) and
    ptz.status.position for the camera's current position.

    Args:
        json_string: The JSON string representation of the camera, as
                     returned by get_camera or get_cameras.
        profile_token: The media profile token to command (see above).
        preset_token: The token of the preset to move to, from
                      camera.ptz.presets in the same JSON.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = camera_from_json(json_string)
    except Exception as e:
        logger.error(f"Failed to parse camera JSON: {e}")
        return f"Failed to parse camera JSON: {e}"

    if not camera.ptz or not camera.ptz.presets:
        return f"Camera at {camera.xaddr} has no PTZ presets available."

    preset = None
    for candidate in camera.ptz.presets:
        if candidate.token == preset_token:
            preset = candidate
            break

    if not preset:
        return f"Preset {preset_token} not found on camera at {camera.xaddr}."

    try:
        camera.errors = None
        goto_preset(camera, profile_token, preset)
        if camera.errors:
            raise Exception(f"Camera returned errors: {camera.errors}")
        return f"Successfully moved camera at {camera.xaddr} to preset {preset_token}."
    except Exception as e:
        logger.error(f"Failed to move camera at {camera.xaddr} to preset {preset_token}: {e}")
        return f"Failed to move camera at {camera.xaddr} to preset {preset_token}: {e}"

@mcp.tool()
async def change_camera_hostname(json_string: str, new_hostname: str) -> str:
    """
    Change the hostname of a camera.

    Args:
        json_string: The JSON string representation of the camera, as returned by get_camera or get_cameras.
        new_hostname: The new hostname to set.

    Returns:
        A message indicating success or failure
    """
    try:
        camera = camera_from_json(json_string)
    except Exception as e:
        logger.error(f"Failed to parse camera JSON: {e}")
        return f"Failed to parse camera JSON: {e}"

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
    Get cameras on the local network.
    
    Args:
        None

    Returns:
        A delimited string containing full camera information in json format 
        for each camera found on the local network. Each camera's information 
        is separated by "\n--\n".
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
    
    logger.debug(f"Found {len(cameras)} {"camera" if len(cameras) == 1 else "cameras"}")

    names = []
    for camera in cameras:
        names.append(camera.to_json())

    return "\n--\n".join(names)

def main():
    logger.debug("Server starting...")
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()