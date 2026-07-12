"""MCP server that bridges Claude to a running Blender instance.

Connects over TCP to the `blender_mcp_addon.py` add-on (which must be
started from inside Blender's sidebar panel) and exposes its commands
as MCP tools.
"""
import base64
import binascii
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP, Image

BLENDER_HOST = os.environ.get("BLENDER_MCP_HOST", "127.0.0.1")
BLENDER_PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))
BLENDER_EXECUTABLE = os.environ.get("BLENDER_MCP_EXECUTABLE") or shutil.which("blender")
CLI_RUNNER_PATH = Path(__file__).resolve().parent.parent / "addon" / "blender_mcp_cli_runner.py"
DEFAULT_TIMEOUT = 120.0
DEFAULT_RENDER_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_render.png")
DEFAULT_THUMBNAIL_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_thumbnail.png")
DEFAULT_VIEWPORT_RENDER_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_viewport_render.png")
DEFAULT_AREA_SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_area_screenshot.png")
DEFAULT_WINDOW_SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_window_screenshot.png")
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SCENE_INFO_LIMIT = 10_000
MAX_RENDER_DIMENSION = 4096
MAX_RENDER_PIXELS = MAX_RENDER_DIMENSION * MAX_RENDER_DIMENSION
MAX_RENDER_SAMPLES = 4096
MAX_THUMBNAIL_DIMENSION = 512
MAX_TIMEOUT = 3600.0
MAX_JOIN_OBJECTS = 256
MAX_UNDO_STEPS = 100
MAX_EXECUTE_CODE_BYTES = 8 * 1024 * 1024
MAX_CLI_STDERR_CHARS = 4000
RETRY_SAFE_COMMANDS = frozenset({
    "get_scene_info", "get_object_info", "get_objects_summary", "get_window_summary",
    "get_blendfile_summary_datablocks", "get_blendfile_summary_missing_files",
    "get_blendfile_summary_linked_libraries", "get_blendfile_summary_path_info",
    "get_blendfile_summary_usage_guess", "get_python_api_docs",
})

mcp = FastMCP("blender")


class BlenderConnection:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self._lock = threading.Lock()

    def _connect(self, timeout: float = DEFAULT_TIMEOUT):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(timeout)
            sock.connect((self.host, self.port))
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self.sock = sock
        self.buf = b""

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""

    def send_command(self, command_type: str, params: dict, timeout: Optional[float] = None) -> dict:
        effective_timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        if (
            isinstance(effective_timeout, bool)
            or not isinstance(effective_timeout, (int, float))
            or not math.isfinite(effective_timeout)
            or effective_timeout <= 0
            or effective_timeout > MAX_TIMEOUT
        ):
            raise ValueError(f"timeout must be greater than 0 and at most {MAX_TIMEOUT:g} seconds")
        deadline = time.monotonic() + effective_timeout
        if not self._lock.acquire(timeout=effective_timeout):
            raise ConnectionError(
                f"Timed out after {effective_timeout:g}s waiting for another Blender command; "
                "no command was sent."
            )
        try:
            request_id = uuid.uuid4().hex
            payload = (
                json.dumps({"id": request_id, "type": command_type, "params": params}) + "\n"
            ).encode("utf-8")
            line = b""
            send_was_attempted = False

            def remaining_timeout() -> float:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("command deadline expired")
                return remaining

            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect(remaining_timeout())
                    self.sock.settimeout(remaining_timeout())
                    send_was_attempted = True
                    self.sock.sendall(payload)
                    while b"\n" not in self.buf:
                        self.sock.settimeout(remaining_timeout())
                        chunk = self.sock.recv(65536)
                        if not chunk:
                            raise ConnectionError("Blender closed the connection")
                        self.buf += chunk
                        newline_index = self.buf.find(b"\n")
                        if (
                            newline_index > MAX_RESPONSE_BYTES
                            or (newline_index < 0 and len(self.buf) > MAX_RESPONSE_BYTES)
                        ):
                            raise ConnectionError(
                                f"Blender response exceeds the {MAX_RESPONSE_BYTES}-byte limit"
                            )
                    line, self.buf = self.buf.split(b"\n", 1)
                    break
                except socket.timeout as exc:
                    self._close()
                    if not send_was_attempted:
                        raise ConnectionError(
                            f"Timed out after {effective_timeout:g}s connecting to Blender at "
                            f"{self.host}:{self.port}; no command was sent."
                        ) from exc
                    raise ConnectionError(
                        f"Timed out after {effective_timeout:g}s waiting for Blender to finish "
                        f"'{command_type}' (request {request_id}). The outcome is unknown: the "
                        "timeout does not cancel Blender work, so it may still complete. Inspect "
                        "the scene before retrying the mutation."
                    ) from exc
                except (ConnectionError, OSError) as exc:
                    self._close()
                    # Inspection calls are intrinsically safe to retry. A
                    # command that never reached sendall is safe too. Mutating
                    # commands are not automatically replayed after sending:
                    # the add-on's response cache is deliberately bounded, so
                    # an unconditional replay could eventually outlive it.
                    if attempt == 0 and (
                        not send_was_attempted or command_type in RETRY_SAFE_COMMANDS
                    ):
                        continue
                    outcome = (
                        " The command outcome is unknown because sending began; Blender may "
                        "still complete it."
                        if send_was_attempted
                        else ""
                    )
                    raise ConnectionError(
                        f"Could not reach Blender at {self.host}:{self.port}. "
                        "Is the Blender MCP Bridge add-on running and its server started? "
                        f"Request: {request_id}. ({exc}){outcome}"
                    ) from exc
            try:
                response = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._close()
                raise ConnectionError(f"Malformed response from Blender: {exc}") from exc
            if not isinstance(response, dict):
                self._close()
                raise ConnectionError(f"Unexpected response shape from Blender: {response!r}")
            if response.get("id") != request_id:
                if response.get("id") is None and response.get("status") == "error":
                    self._close()
                    raise RuntimeError(response.get("error", "Blender rejected the connection"))
                self._close()
                raise ConnectionError(
                    "Response request ID mismatch; update/restart both Blender MCP components "
                    f"(expected {request_id!r}, got {response.get('id')!r})"
                )
            if response.get("status") != "ok":
                error = response.get("error", "Unknown error from Blender")
                traceback_str = response.get("traceback")
                if traceback_str:
                    error = f"{error}\n\n{traceback_str}"
                raise RuntimeError(error)
            return response.get("result", {})
        finally:
            self._lock.release()


_conn = BlenderConnection(BLENDER_HOST, BLENDER_PORT)


def _run_cli_command(command_type: str, params: dict, blend_file: str, timeout: float) -> Any:
    """Run one command against blend_file in a background `blender --background`
    subprocess, independent of any running interactive session. Unlike
    _conn.send_command, there is no server to reconnect to and nothing to
    retry - each call is a fresh, self-contained process."""
    if not isinstance(blend_file, str) or not blend_file.strip():
        raise TypeError("blend_file must be a non-empty string")
    if not os.path.isfile(blend_file):
        raise FileNotFoundError(f"No such .blend file: {blend_file}")
    if not BLENDER_EXECUTABLE:
        raise RuntimeError(
            "Could not find a Blender executable on PATH; set BLENDER_MCP_EXECUTABLE "
            "to its full path."
        )
    if not CLI_RUNNER_PATH.is_file():
        raise RuntimeError(f"CLI runner script is missing: {CLI_RUNNER_PATH}")

    params_b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
    output_path = os.path.join(tempfile.gettempdir(), f"blender_mcp_cli_{uuid.uuid4().hex}.json")
    try:
        try:
            proc = subprocess.run(
                [
                    BLENDER_EXECUTABLE, "--background", "--factory-startup", blend_file,
                    "--python-exit-code", "1",
                    "--python", str(CLI_RUNNER_PATH),
                    "--", command_type, params_b64, output_path,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Blender did not finish '{command_type}' on {blend_file} "
                f"within {timeout}s; its outcome is unknown"
            ) from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"Blender exited with code {proc.returncode} running '{command_type}' "
                f"on {blend_file}: {proc.stderr[-MAX_CLI_STDERR_CHARS:]}"
            )
        if not os.path.isfile(output_path):
            raise RuntimeError(
                f"Blender produced no result for '{command_type}' on {blend_file}: "
                f"{proc.stderr[-MAX_CLI_STDERR_CHARS:]}"
            )
        with open(output_path, "r", encoding="utf-8") as f:
            response = json.load(f)
        if response.get("status") != "ok":
            raise RuntimeError(response.get("error") or "Unknown CLI command error")
        return response.get("result")
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


def _bounded_int(value: int, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _finite_number(
    value: float, name: str, *, minimum: Optional[float] = None, maximum: Optional[float] = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"{name} must be a finite number")
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _decode_png_result(result: dict) -> Image:
    try:
        encoded = result["image_base64"]
        if not isinstance(encoded, str):
            raise TypeError("image_base64 is not a string")
        data = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ConnectionError(f"Blender returned malformed PNG image data: {exc}") from exc
    return Image(data=data, format="png")


@mcp.tool()
def get_scene_info(limit: int = 200) -> dict:
    """Get a summary of every object in the current Blender scene, including
    transforms, dimensions, and which object is active. At most `limit`
    objects are returned; `object_count`/`truncated` report the full size."""
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_SCENE_INFO_LIMIT)
    return _conn.send_command("get_scene_info", {"limit": limit})


@mcp.tool()
def get_object_info(name: str) -> dict:
    """Get detailed info about one object: transform, mesh stats, modifiers,
    assigned materials, constraints, child object names, its data-block name,
    and the collections it belongs to."""
    return _conn.send_command("get_object_info", {"name": name})


PrimitiveType = Literal["cube", "sphere", "ico_sphere", "cylinder", "cone", "plane", "torus", "monkey"]
LightType = Literal["POINT", "SUN", "SPOT", "AREA"]
MirrorAxis = Literal["X", "Y", "Z"]


@mcp.tool()
def add_primitive(
    type: PrimitiveType,
    name: Optional[str] = None,
    location: tuple[float, float, float] = (0, 0, 0),
    rotation: tuple[float, float, float] = (0, 0, 0),
    scale: tuple[float, float, float] = (1, 1, 1),
) -> dict:
    """Add a primitive mesh to the scene."""
    params: dict[str, Any] = {"type": type, "location": list(location), "rotation": list(rotation), "scale": list(scale)}
    if name:
        params["name"] = name
    return _conn.send_command("add_primitive", params)


@mcp.tool()
def delete_object(name: str) -> dict:
    """Delete the named object from the scene."""
    return _conn.send_command("delete_object", {"name": name})


@mcp.tool()
def set_transform(
    name: str,
    location: Optional[tuple[float, float, float]] = None,
    rotation: Optional[tuple[float, float, float]] = None,
    scale: Optional[tuple[float, float, float]] = None,
) -> dict:
    """Set location/rotation(XYZ euler radians)/scale on an existing object;
    values are local-space if the object is parented. Rotation is applied
    correctly whatever the object's rotation mode is. Any argument left as
    None is unchanged."""
    params: dict[str, Any] = {"name": name}
    if location is not None:
        params["location"] = list(location)
    if rotation is not None:
        params["rotation"] = list(rotation)
    if scale is not None:
        params["scale"] = list(scale)
    return _conn.send_command("set_transform", params)


@mcp.tool()
def create_material(
    name: str,
    base_color: Optional[tuple[float, float, float]] = None,
    metallic: Optional[float] = None,
    roughness: Optional[float] = None,
    emission_color: Optional[tuple[float, float, float]] = None,
    emission_strength: Optional[float] = None,
    alpha: Optional[float] = None,
    ior: Optional[float] = None,
    transmission: Optional[float] = None,
    specular: Optional[float] = None,
    coat: Optional[float] = None,
    sheen: Optional[float] = None,
    subsurface: Optional[float] = None,
) -> dict:
    """Create (or update) a Principled-BSDF material. Colors are RGB 0-1.
    Scalar weights (0-1): transmission makes glass (pair with low roughness
    and ior ~1.45), alpha < 1 makes it transparent (the material is switched
    to blended mode automatically), specular sets Specular IOR Level, and
    coat/sheen/subsurface set the matching BSDF weights."""
    params: dict[str, Any] = {"name": name}
    scalars = {
        "metallic": metallic,
        "roughness": roughness,
        "emission_strength": emission_strength,
        "alpha": alpha,
        "ior": ior,
        "transmission": transmission,
        "specular": specular,
        "coat": coat,
        "sheen": sheen,
        "subsurface": subsurface,
    }
    for key, value in scalars.items():
        if value is not None:
            params[key] = value
    if base_color is not None:
        params["base_color"] = list(base_color)
    if emission_color is not None:
        params["emission_color"] = list(emission_color)
    return _conn.send_command("create_material", params)


@mcp.tool()
def assign_material(object_name: str, material_name: str) -> dict:
    """Assign an existing material to an object's first material slot."""
    return _conn.send_command("assign_material", {"object_name": object_name, "material_name": material_name})


@mcp.tool()
def add_light(
    type: LightType = "POINT",
    name: str = "Light",
    location: tuple[float, float, float] = (0, 0, 5),
    energy: Optional[float] = None,
    color: Optional[tuple[float, float, float]] = None,
    rotation: Optional[tuple[float, float, float]] = None,
) -> dict:
    """Add a light to the scene. energy defaults to 1000 W for point/spot/area
    lights and 3 for SUN lights (sun strength is irradiance in W/m^2, so
    hundreds would blow out the scene). rotation (XYZ euler radians) aims
    directional lights - they point straight down -Z at zero rotation."""
    params: dict[str, Any] = {"type": type, "name": name, "location": list(location)}
    if energy is not None:
        params["energy"] = energy
    if color is not None:
        params["color"] = list(color)
    if rotation is not None:
        params["rotation"] = list(rotation)
    return _conn.send_command("add_light", params)


@mcp.tool()
def set_camera(
    name: str = "Camera",
    location: Optional[tuple[float, float, float]] = None,
    rotation: Optional[tuple[float, float, float]] = None,
    look_at: Optional[tuple[float, float, float]] = None,
    lens: Optional[float] = None,
    make_active: bool = True,
) -> dict:
    """Create or update a camera and optionally make it the active scene
    camera. look_at aims the camera at a world-space point (applied after
    location, mutually exclusive with rotation) - prefer it over computing
    euler angles by hand."""
    params: dict[str, Any] = {"name": name, "make_active": make_active}
    if location is not None:
        params["location"] = list(location)
    if rotation is not None:
        params["rotation"] = list(rotation)
    if look_at is not None:
        params["look_at"] = list(look_at)
    if lens is not None:
        params["lens"] = lens
    return _conn.send_command("set_camera", params)


@mcp.tool()
def render_scene(
    filepath: str = DEFAULT_RENDER_PATH,
    resolution_x: int = 1024,
    resolution_y: int = 1024,
    samples: int = 64,
    timeout: float = 600,
) -> Image:
    """Render the current scene to a PNG and return it as an image. The
    scene's own render settings are restored afterwards. Increase timeout
    (seconds) for high-sample-count or high-resolution renders that take
    longer than the default."""
    resolution_x = _bounded_int(
        resolution_x, "resolution_x", minimum=1, maximum=MAX_RENDER_DIMENSION
    )
    resolution_y = _bounded_int(
        resolution_y, "resolution_y", minimum=1, maximum=MAX_RENDER_DIMENSION
    )
    if resolution_x * resolution_y > MAX_RENDER_PIXELS:
        raise ValueError(f"render size must not exceed {MAX_RENDER_PIXELS} total pixels")
    samples = _bounded_int(samples, "samples", minimum=1, maximum=MAX_RENDER_SAMPLES)
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    result = _conn.send_command(
        "render_scene",
        {
            "filepath": filepath,
            "resolution_x": resolution_x,
            "resolution_y": resolution_y,
            "samples": samples,
            "return_image": True,
        },
        timeout=timeout,
    )
    return _decode_png_result(result)


@mcp.tool()
def get_viewport_screenshot() -> Image:
    """Capture a screenshot of Blender's 3D viewport using its current
    on-screen shading mode (solid/material/rendered - whatever is active)."""
    result = _conn.send_command("get_viewport_screenshot", {})
    return _decode_png_result(result)


@mcp.tool()
def add_capsule(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float = 0.1,
    name: Optional[str] = None,
    caps: bool = True,
) -> dict:
    """Add a cylinder (optionally with rounded sphere caps) aligned between two
    world-space points. Use this for limbs/bones instead of hand-rotating a
    cylinder with Euler angles - it handles the alignment math for you."""
    radius = _finite_number(radius, "radius", minimum=1.0e-9, maximum=1_000_000.0)
    if not isinstance(caps, bool):
        raise TypeError("caps must be a boolean")
    params: dict[str, Any] = {
        "start": list(start),
        "end": list(end),
        "radius": radius,
        "caps": caps,
    }
    if name:
        params["name"] = name
    return _conn.send_command("add_capsule", params)


@mcp.tool()
def mirror_object(name: str, axis: MirrorAxis = "X", new_name: Optional[str] = None) -> dict:
    """Duplicate an object and reflect its world-space transform across the
    plane through the world origin perpendicular to the given axis (X/Y/Z),
    exactly like Blender's Object > Mirror. Useful for generating the other
    half of a symmetric part (e.g. mirror 'Arm_L' to get 'Arm_R'). The copy
    renders correctly as-is; join_objects fixes face winding automatically
    if you later join it into another mesh."""
    params: dict[str, Any] = {"name": name, "axis": axis}
    if new_name:
        params["new_name"] = new_name
    return _conn.send_command("mirror_object", params)


@mcp.tool()
def parent_object(child: str, parent: str, keep_transform: bool = True) -> dict:
    """Parent one object to another. If keep_transform is True, the child's
    current world-space position/rotation/scale is preserved."""
    return _conn.send_command(
        "parent_object", {"child": child, "parent": parent, "keep_transform": keep_transform}
    )


@mcp.tool()
def join_objects(names: list[str], target_name: Optional[str] = None) -> dict:
    """Join multiple mesh objects into one. The first name in the list becomes
    the base object unless target_name is given to rename the result. Sources
    with mirrored (negative-determinant) transforms get their face winding
    corrected during the join so normals stay outward."""
    if not isinstance(names, list):
        raise TypeError("names must be a list")
    if not 2 <= len(names) <= MAX_JOIN_OBJECTS:
        raise ValueError(f"names must contain between 2 and {MAX_JOIN_OBJECTS} objects")
    params: dict[str, Any] = {"names": names}
    if target_name:
        params["target_name"] = target_name
    return _conn.send_command("join_objects", params)


@mcp.tool()
def set_shading(name: str, smooth: bool = True) -> dict:
    """Set smooth (True) or flat (False) shading on every face of a mesh
    object - e.g. smooth-shade spheres and capsules used as limbs."""
    return _conn.send_command("set_shading", {"name": name, "smooth": smooth})


@mcp.tool()
def undo(steps: int = 1) -> dict:
    """Undo the last N Blender undo steps. Best-effort: stops early if there
    is nothing left to undo. Most MCP commands push one undo step, but a
    compound operator-backed command such as join_objects may push several
    internal steps and need more than one `undo` call to fully reverse."""
    steps = _bounded_int(steps, "steps", minimum=1, maximum=MAX_UNDO_STEPS)
    return _conn.send_command("undo", {"steps": steps})


@mcp.tool()
def redo(steps: int = 1) -> dict:
    """Redo the last N undone steps. Best-effort: stops early when there is
    nothing left to redo."""
    steps = _bounded_int(steps, "steps", minimum=1, maximum=MAX_UNDO_STEPS)
    return _conn.send_command("redo", {"steps": steps})


@mcp.tool()
def execute_code(code: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Escape hatch: run arbitrary Python inside Blender. `bpy`, `bmesh`,
    `mathutils`, `Vector`, `Matrix`, `Euler` and `Quaternion` are predefined.
    Assign to a variable named `result` to return data (non-JSON-serializable
    results come back as their repr). `print()` output is captured and
    returned as `stdout`/`stderr` (each capped, with a trailing truncation
    marker if exceeded) - handy for debugging while iterating on a script.
    Use for anything not covered by the other tools (modifiers, geometry
    nodes, UV work, etc.); raise timeout (seconds) for long-running scripts
    like physics bakes. A few destructive calls (`bpy.ops.wm.quit_blender`
    and preference/startup-file resets) and `sys.exit()` are blocked as a
    guardrail against accidental self-inflicted damage - not a real sandbox,
    just cheap insurance against the obvious mistakes."""
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    if len(code.encode("utf-8")) > MAX_EXECUTE_CODE_BYTES:
        raise ValueError(f"code must be at most {MAX_EXECUTE_CODE_BYTES} UTF-8 bytes")
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _conn.send_command("execute_code", {"code": code}, timeout=timeout)


@mcp.tool()
def save_file(filepath: Optional[str] = None) -> dict:
    """Save the Blender scene. With filepath, does save-as to that .blend
    path; without it, saves in place (errors if the file has never been
    saved)."""
    params: dict[str, Any] = {}
    if filepath is not None:
        if not isinstance(filepath, str):
            raise TypeError("filepath must be a string")
        if not filepath.strip():
            raise ValueError("filepath must not be empty")
        params["filepath"] = filepath
    return _conn.send_command("save_file", params)


@mcp.tool()
def get_objects_summary() -> dict:
    """Get the scene's collection hierarchy (nested collections and the
    objects directly in each one), unlike get_scene_info's flat object list."""
    return _conn.send_command("get_objects_summary", {})


@mcp.tool()
def get_window_summary() -> dict:
    """Get a JSON description of Blender's window layout: open windows,
    their areas (type/position/size), current interaction mode, active
    object, and selection."""
    return _conn.send_command("get_window_summary", {})


@mcp.tool()
def jump_to_view3d_object(name: str) -> dict:
    """Select the named object, make it active, and frame it in the 3D
    viewport (like pressing Numpad-. after selecting it)."""
    return _conn.send_command("jump_to_view3d_object", {"name": name})


@mcp.tool()
def render_thumbnail(
    filepath: str = DEFAULT_THUMBNAIL_PATH,
    size: int = 128,
    timeout: float = 60,
) -> Image:
    """Render a small, fast preview of the current scene (Workbench engine,
    no sample convergence) and return it as an image. For a full-quality
    render use render_scene instead."""
    size = _bounded_int(size, "size", minimum=1, maximum=MAX_THUMBNAIL_DIMENSION)
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    result = _conn.send_command(
        "render_thumbnail",
        {"filepath": filepath, "size": size, "return_image": True},
        timeout=timeout,
    )
    return _decode_png_result(result)


@mcp.tool()
def get_blendfile_summary_datablocks() -> dict:
    """Get data-block counts by type (meshes, materials, images, etc.),
    the active workspace, and the current render engine."""
    return _conn.send_command("get_blendfile_summary_datablocks", {})


@mcp.tool()
def get_blendfile_summary_missing_files() -> dict:
    """Find external file references (images, libraries, fonts, sounds,
    movie clips, cache files) that point to a path missing on disk."""
    return _conn.send_command("get_blendfile_summary_missing_files", {})


@mcp.tool()
def get_blendfile_summary_linked_libraries() -> dict:
    """Get the tree of directly and indirectly linked library (.blend)
    files this file depends on."""
    return _conn.send_command("get_blendfile_summary_linked_libraries", {})


@mcp.tool()
def get_blendfile_summary_path_info() -> dict:
    """Get the current file's path, save status, unsaved-changes flag, size,
    time since last save, and local backup count."""
    return _conn.send_command("get_blendfile_summary_path_info", {})


@mcp.tool()
def get_blendfile_summary_usage_guess() -> dict:
    """Get a heuristic, scored guess at what this file is used for (character
    rigging, procedural geometry nodes, video editing, compositing, 2D
    grease-pencil animation, or a static look-dev asset) based on what kinds
    of data it contains. Approximate - treat it as a starting hint, not fact."""
    return _conn.send_command("get_blendfile_summary_usage_guess", {})


@mcp.tool()
def get_python_api_docs(identifier: str) -> dict:
    """Look up a bpy Python API identifier at runtime (e.g.
    'bpy.types.Object', 'bpy.types.Object.location', 'bpy.ops.mesh.primitive_cube_add').
    Returns its docstring plus, for RNA types, its properties/functions. End
    an identifier with '*' after a trailing dot to list matches, e.g.
    'bpy.types.Mesh*' or 'bpy.ops.mesh.*'."""
    return _conn.send_command("get_python_api_docs", {"identifier": identifier})


@mcp.tool()
def render_viewport_to_path(
    filepath: str = DEFAULT_VIEWPORT_RENDER_PATH,
    timeout: float = 600,
) -> Image:
    """Render using whatever engine/resolution/samples the scene already has
    configured, without overriding them. Unlike render_scene (always
    overrides resolution/samples) and render_thumbnail (always forces a fast
    low-quality preview), this reflects exactly what a normal Render >
    Render Image would produce right now."""
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    result = _conn.send_command(
        "render_viewport", {"filepath": filepath, "return_image": True}, timeout=timeout
    )
    return _decode_png_result(result)


@mcp.tool()
def get_screenshot_of_area_as_image(area_type: str = "VIEW_3D") -> Image:
    """Capture a screenshot of one editor area by type (e.g. 'VIEW_3D',
    'NODE_EDITOR', 'IMAGE_EDITOR', 'PROPERTIES', 'OUTLINER') - the first area
    of that type found across all open windows. Use get_viewport_screenshot
    for the common 3D-viewport case; this covers any other editor."""
    result = _conn.send_command("get_screenshot_of_area", {"area_type": area_type})
    return _decode_png_result(result)


@mcp.tool()
def get_screenshot_of_window_as_image() -> Image:
    """Capture a screenshot of the entire Blender window (every visible area
    combined), unlike get_viewport_screenshot/get_screenshot_of_area_as_image
    which capture a single area."""
    result = _conn.send_command("get_screenshot_of_window", {})
    return _decode_png_result(result)


@mcp.tool()
def jump_to_view3d_object_data(name: str) -> dict:
    """Select and frame in the 3D viewport whichever object uses the
    data-block named `name` (e.g. a mesh, curve, or armature data name),
    rather than looking up an object by its own name like
    jump_to_view3d_object does. Useful when several objects share one
    mesh/data-block and you want to find them by that shared data's name."""
    return _conn.send_command("jump_to_view3d_object_data", {"name": name})


@mcp.tool()
def jump_to_tab_by_name(name: str) -> dict:
    """Switch every open window's active workspace tab to `name` (e.g.
    'Shading', 'UV Editing', 'Animation', 'Scripting')."""
    return _conn.send_command("jump_to_tab_by_name", {"name": name})


@mcp.tool()
def jump_to_tab_by_space_type(space_type: str) -> dict:
    """Switch to whichever workspace has an area of the given editor type
    (e.g. 'NODE_EDITOR', 'IMAGE_EDITOR', 'SEQUENCE_EDITOR') - use this when
    you know what kind of editor you need but not which workspace tab has
    it."""
    return _conn.send_command("jump_to_tab_by_space_type", {"space_type": space_type})


# ---------------------------------------------------------------------------
# CLI/background mode: runs a command against a .blend file in a fresh
# `blender --background` subprocess instead of the running interactive
# session. Useful for inspecting files Blender doesn't currently have open,
# or when no interactive session is running at all. There is no undo/replay
# protection here - each call spawns and tears down its own Blender process.
# ---------------------------------------------------------------------------

@mcp.tool()
def get_blendfile_summary_datablocks_for_cli(blend_file: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Like get_blendfile_summary_datablocks, but opens blend_file in a
    background Blender process instead of using the running interactive
    session - works even if Blender isn't open, or has a different file
    loaded."""
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _run_cli_command("get_blendfile_summary_datablocks", {}, blend_file, timeout)


@mcp.tool()
def get_blendfile_summary_missing_files_for_cli(blend_file: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Like get_blendfile_summary_missing_files, but opens blend_file in a
    background Blender process instead of using the running interactive
    session."""
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _run_cli_command("get_blendfile_summary_missing_files", {}, blend_file, timeout)


@mcp.tool()
def get_blendfile_summary_linked_libraries_for_cli(blend_file: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Like get_blendfile_summary_linked_libraries, but opens blend_file in a
    background Blender process instead of using the running interactive
    session."""
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _run_cli_command("get_blendfile_summary_linked_libraries", {}, blend_file, timeout)


@mcp.tool()
def get_blendfile_summary_path_info_for_cli(blend_file: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Like get_blendfile_summary_path_info, but opens blend_file in a
    background Blender process instead of using the running interactive
    session."""
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _run_cli_command("get_blendfile_summary_path_info", {}, blend_file, timeout)


@mcp.tool()
def get_blendfile_summary_usage_guess_for_cli(blend_file: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Like get_blendfile_summary_usage_guess, but opens blend_file in a
    background Blender process instead of using the running interactive
    session."""
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _run_cli_command("get_blendfile_summary_usage_guess", {}, blend_file, timeout)


@mcp.tool()
def execute_code_for_cli(blend_file: str, code: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Like execute_code, but runs against blend_file in a background Blender
    process instead of the running interactive session, with nobody watching
    it happen. The file is NOT saved automatically - call
    bpy.ops.wm.save_mainfile() yourself in code if you want changes kept.
    Prefer execute_code for anything you can run interactively; use this only
    when Blender isn't open, or you need to touch a file other than the one
    currently loaded."""
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    if len(code.encode("utf-8")) > MAX_EXECUTE_CODE_BYTES:
        raise ValueError(f"code must be at most {MAX_EXECUTE_CODE_BYTES} UTF-8 bytes")
    timeout = _finite_number(timeout, "timeout", minimum=1.0e-6, maximum=MAX_TIMEOUT)
    return _run_cli_command("execute_code", {"code": code}, blend_file, timeout)


if __name__ == "__main__":
    mcp.run(transport="stdio")
