"""MCP server that bridges Claude to a running Blender instance.

Connects over TCP to the `blender_mcp_addon.py` add-on (which must be
started from inside Blender's sidebar panel) and exposes its commands
as MCP tools.
"""
import base64
import json
import os
import socket
import threading
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP, Image

BLENDER_HOST = os.environ.get("BLENDER_MCP_HOST", "127.0.0.1")
BLENDER_PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))

mcp = FastMCP("blender")


class BlenderConnection:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self._lock = threading.Lock()

    def _ensure_connected(self):
        if self.sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(120)
            sock.connect((self.host, self.port))
            self.sock = sock

    def send_command(self, command_type: str, params: dict) -> dict:
        with self._lock:
            try:
                self._ensure_connected()
                payload = json.dumps({"type": command_type, "params": params}) + "\n"
                self.sock.sendall(payload.encode("utf-8"))
                while b"\n" not in self.buf:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("Blender closed the connection")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                response = json.loads(line.decode("utf-8"))
            except (ConnectionError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                if self.sock is not None:
                    self.sock.close()
                self.sock = None
                self.buf = b""
                raise ConnectionError(
                    f"Could not reach Blender at {self.host}:{self.port}. "
                    "Is the Blender MCP Bridge add-on running and its server started? "
                    f"({exc})"
                ) from exc
            if response.get("status") != "ok":
                error = response.get("error", "Unknown error from Blender")
                traceback_str = response.get("traceback")
                if traceback_str:
                    error = f"{error}\n\n{traceback_str}"
                raise RuntimeError(error)
            return response.get("result", {})


_conn = BlenderConnection(BLENDER_HOST, BLENDER_PORT)


@mcp.tool()
def get_scene_info() -> dict:
    """Get a summary of every object in the current Blender scene, including
    transforms, dimensions, and which object is active."""
    return _conn.send_command("get_scene_info", {})


@mcp.tool()
def get_object_info(name: str) -> dict:
    """Get detailed info about one object: transform, mesh stats,
    modifiers, and assigned materials."""
    return _conn.send_command("get_object_info", {"name": name})


@mcp.tool()
def add_primitive(
    type: str,
    name: Optional[str] = None,
    location: tuple[float, float, float] = (0, 0, 0),
    rotation: tuple[float, float, float] = (0, 0, 0),
    scale: tuple[float, float, float] = (1, 1, 1),
) -> dict:
    """Add a primitive mesh to the scene.

    type must be one of: cube, sphere, ico_sphere, cylinder, cone, plane, torus, monkey.
    """
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
    """Set location/rotation(euler radians)/scale on an existing object.
    Any argument left as None is unchanged."""
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
) -> dict:
    """Create (or update) a Principled-BSDF material. Colors are RGB 0-1."""
    params: dict[str, Any] = {"name": name}
    if base_color is not None:
        params["base_color"] = list(base_color)
    if metallic is not None:
        params["metallic"] = metallic
    if roughness is not None:
        params["roughness"] = roughness
    if emission_color is not None:
        params["emission_color"] = list(emission_color)
    if emission_strength is not None:
        params["emission_strength"] = emission_strength
    return _conn.send_command("create_material", params)


@mcp.tool()
def assign_material(object_name: str, material_name: str) -> dict:
    """Assign an existing material to an object's first material slot."""
    return _conn.send_command("assign_material", {"object_name": object_name, "material_name": material_name})


@mcp.tool()
def add_light(
    type: str = "POINT",
    name: str = "Light",
    location: tuple[float, float, float] = (0, 0, 5),
    energy: float = 1000.0,
    color: Optional[tuple[float, float, float]] = None,
) -> dict:
    """Add a light to the scene. type is one of POINT, SUN, SPOT, AREA."""
    params: dict[str, Any] = {"type": type, "name": name, "location": list(location), "energy": energy}
    if color is not None:
        params["color"] = list(color)
    return _conn.send_command("add_light", params)


@mcp.tool()
def set_camera(
    name: str = "Camera",
    location: Optional[tuple[float, float, float]] = None,
    rotation: Optional[tuple[float, float, float]] = None,
    lens: Optional[float] = None,
    make_active: bool = True,
) -> dict:
    """Create or update a camera and optionally make it the active scene camera."""
    params: dict[str, Any] = {"name": name, "make_active": make_active}
    if location is not None:
        params["location"] = list(location)
    if rotation is not None:
        params["rotation"] = list(rotation)
    if lens is not None:
        params["lens"] = lens
    return _conn.send_command("set_camera", params)


@mcp.tool()
def render_scene(
    filepath: str = "/tmp/blender_mcp_render.png",
    resolution_x: int = 1024,
    resolution_y: int = 1024,
    samples: int = 64,
) -> Image:
    """Render the current scene to a PNG and return it as an image."""
    result = _conn.send_command(
        "render_scene",
        {
            "filepath": filepath,
            "resolution_x": resolution_x,
            "resolution_y": resolution_y,
            "samples": samples,
            "return_image": True,
        },
    )
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


@mcp.tool()
def get_viewport_screenshot() -> Image:
    """Capture a screenshot of Blender's 3D viewport (fast, unlit, for quick feedback)."""
    result = _conn.send_command("get_viewport_screenshot", {})
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


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
def mirror_object(name: str, axis: str = "X", new_name: Optional[str] = None) -> dict:
    """Duplicate an object and mirror it across the given local axis (X/Y/Z),
    flipping normals so it renders correctly. Location is also mirrored about
    world origin on that axis. Useful for generating the other half of a
    symmetric part (e.g. mirror 'Arm_L' to get 'Arm_R')."""
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
    the base object unless target_name is given to rename the result."""
    params: dict[str, Any] = {"names": names}
    if target_name:
        params["target_name"] = target_name
    return _conn.send_command("join_objects", params)


@mcp.tool()
def undo(steps: int = 1) -> dict:
    """Undo the last N operations in Blender. Best-effort: stops early if
    there is nothing left to undo."""
    return _conn.send_command("undo", {"steps": steps})


@mcp.tool()
def execute_code(code: str) -> dict:
    """Escape hatch: run arbitrary Python inside Blender with `bpy` available.
    Assign to a variable named `result` to return data. Use for anything not
    covered by the other tools (bmesh editing, modifiers, geometry nodes, etc.)."""
    return _conn.send_command("execute_code", {"code": code})


@mcp.tool()
def save_file(filepath: str) -> dict:
    """Save the current Blender scene to a .blend file at filepath."""
    return _conn.send_command("save_file", {"filepath": filepath})


if __name__ == "__main__":
    mcp.run(transport="stdio")
