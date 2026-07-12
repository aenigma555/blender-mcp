bl_info = {
    "name": "Blender MCP Bridge",
    "author": "claude",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > MCP",
    "description": "Runs a local TCP command server so an external MCP server can drive Blender",
    "category": "Interface",
}

import base64
from collections import OrderedDict
import hashlib
import inspect
import json
import math
import os
import queue
import socket
import tempfile
import threading
import time
import traceback

import bmesh
import bpy
import mathutils
from mathutils import Euler, Matrix, Quaternion, Vector

HOST = "127.0.0.1"
MAX_LINE_BYTES = 64 * 1024 * 1024  # safety cap on a single buffered command line
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PENDING_COMMANDS = 128
MAX_CLIENTS = 8
CLIENT_SOCKET_TIMEOUT = 300.0
MAX_REQUEST_ID_BYTES = 128
MAX_DEDUP_ENTRIES = 2048
MAX_DEDUP_CACHE_BYTES = 128 * 1024 * 1024
DEDUP_TTL_SECONDS = 15 * 60
MAX_SCENE_INFO_LIMIT = 10_000
MAX_RENDER_DIMENSION = 4096
MAX_RENDER_PIXELS = MAX_RENDER_DIMENSION * MAX_RENDER_DIMENSION
MAX_RENDER_SAMPLES = 4096
MAX_JOIN_OBJECTS = 256
MAX_UNDO_STEPS = 100
MAX_EXECUTE_CODE_BYTES = 8 * 1024 * 1024
MAX_THUMBNAIL_DIMENSION = 512
MAX_API_DOCS_MATCHES = 200

_server_socket = None
_server_thread = None
_running = False
_bound_port = None
_command_queue = queue.Queue(maxsize=MAX_PENDING_COMMANDS)
_timer_registered = False
_client_sockets_lock = threading.Lock()
_client_sockets = {}
_generation = 0
_last_server_error = None
_CACHE_NAMESPACE_KEY = "_blender_mcp_response_cache_v3"
_response_cache = bpy.app.driver_namespace.get(_CACHE_NAMESPACE_KEY)
if not isinstance(_response_cache, OrderedDict):
    _response_cache = OrderedDict()
    bpy.app.driver_namespace[_CACHE_NAMESPACE_KEY] = _response_cache
_response_cache_bytes = sum(entry[3] for entry in _response_cache.values())


# ---------------------------------------------------------------------------
# Command handlers (always executed on Blender's main thread via the timer)
# ---------------------------------------------------------------------------

def _require_mapping(value, name):
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _require_string(value, name, *, allow_empty=False, max_length=1024):
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return value


def _require_bool(value, name):
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _require_finite_number(value, name, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _require_int(value, name, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _require_vector(value, name, *, size=3, minimum=None, maximum=None):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise TypeError(f"{name} must contain exactly {size} numbers")
    return tuple(
        _require_finite_number(component, f"{name}[{index}]", minimum=minimum, maximum=maximum)
        for index, component in enumerate(value)
    )


def _require_name(params, key="name"):
    return _require_string(params[key], key)

def _get_scene_object(name):
    return bpy.context.scene.objects.get(name)


def _require_object_mode():
    if bpy.context.mode != 'OBJECT':
        raise RuntimeError(
            f"Blender is in '{bpy.context.mode}' mode; switch to Object Mode before running this command"
        )


def _apply_rotation(obj, euler_xyz):
    """Set an object's orientation from XYZ euler radians, whatever its
    rotation_mode is (assigning rotation_euler on a quaternion-mode object
    would be silently ignored)."""
    quat = Euler(euler_xyz, 'XYZ').to_quaternion()
    if obj.rotation_mode == 'QUATERNION':
        obj.rotation_quaternion = quat
    elif obj.rotation_mode == 'AXIS_ANGLE':
        axis, angle = quat.to_axis_angle()
        obj.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
    else:
        obj.rotation_euler = quat.to_euler(obj.rotation_mode)


def _obj_summary(obj):
    mode = obj.rotation_mode
    if mode == 'QUATERNION':
        rotation_euler = list(obj.rotation_quaternion.to_euler())
    elif mode == 'AXIS_ANGLE':
        angle, x, y, z = obj.rotation_axis_angle
        rotation_euler = list(Quaternion((x, y, z), angle).to_euler())
    else:
        rotation_euler = list(obj.rotation_euler)
    return {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_mode": mode,
        "rotation_euler": rotation_euler,
        "rotation_quaternion": list(obj.rotation_quaternion) if mode == 'QUATERNION' else None,
        "scale": list(obj.scale),
        "dimensions": list(obj.dimensions),
        "visible": obj.visible_get(),
        "parent": obj.parent.name if obj.parent else None,
    }


def cmd_get_scene_info(params):
    _require_mapping(params, "params")
    limit = _require_int(
        params.get("limit", 200), "limit", minimum=1, maximum=MAX_SCENE_INFO_LIMIT
    )
    scene = bpy.context.scene
    objects = list(scene.objects)
    return {
        "scene_name": scene.name,
        "frame_current": scene.frame_current,
        "object_count": len(objects),
        "truncated": len(objects) > limit,
        "objects": [_obj_summary(o) for o in objects[:limit]],
        "active_object": scene.view_layers[0].objects.active.name
        if scene.view_layers[0].objects.active else None,
    }


def cmd_get_object_info(params):
    _require_mapping(params, "params")
    name = _require_name(params)
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    info = _obj_summary(obj)
    if obj.type == "MESH":
        mesh = obj.data
        info["mesh"] = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
        }
    info["modifiers"] = [{"name": m.name, "type": m.type} for m in obj.modifiers]
    # Material slots may be empty (None); report them as null rather than crash.
    info["materials"] = (
        [m.name if m else None for m in obj.data.materials]
        if obj.data is not None and hasattr(obj.data, "materials") else []
    )
    return info


_PRIMITIVES = {
    "cube": lambda **kw: bpy.ops.mesh.primitive_cube_add(**kw),
    "sphere": lambda **kw: bpy.ops.mesh.primitive_uv_sphere_add(**kw),
    "ico_sphere": lambda **kw: bpy.ops.mesh.primitive_ico_sphere_add(**kw),
    "cylinder": lambda **kw: bpy.ops.mesh.primitive_cylinder_add(**kw),
    "cone": lambda **kw: bpy.ops.mesh.primitive_cone_add(**kw),
    "plane": lambda **kw: bpy.ops.mesh.primitive_plane_add(**kw),
    "torus": lambda **kw: bpy.ops.mesh.primitive_torus_add(**kw),
    "monkey": lambda **kw: bpy.ops.mesh.primitive_monkey_add(**kw),
}


def cmd_add_primitive(params):
    _require_object_mode()
    _require_mapping(params, "params")
    prim_type = _require_string(params["type"], "type")
    if prim_type not in _PRIMITIVES:
        raise ValueError(f"Unknown primitive type '{prim_type}'. Options: {list(_PRIMITIVES)}")
    location = _require_vector(params.get("location", (0, 0, 0)), "location")
    rotation = _require_vector(params.get("rotation", (0, 0, 0)), "rotation")
    scale = _require_vector(params.get("scale", (1, 1, 1)), "scale")
    name = _require_string(params["name"], "name") if "name" in params else None
    previous_selection = _save_selection()
    objects_before = set(bpy.data.objects)
    meshes_before = set(bpy.data.meshes)
    try:
        add_result = _PRIMITIVES[prim_type](location=location, rotation=rotation)
        if 'FINISHED' not in add_result:
            raise RuntimeError(f"Failed to add {prim_type} (result: {add_result!r})")
        obj = bpy.context.active_object
        if obj is None or obj in objects_before:
            raise RuntimeError(f"Added a {prim_type} but no new active object resulted")
        obj.scale = scale
        if name is not None:
            obj.name = name
    except BaseException:
        for created in set(bpy.data.objects) - objects_before:
            bpy.data.objects.remove(created, do_unlink=True)
        for created_mesh in set(bpy.data.meshes) - meshes_before:
            if created_mesh.users == 0:
                bpy.data.meshes.remove(created_mesh)
        _restore_selection(previous_selection)
        raise
    return _obj_summary(obj)


def cmd_delete_object(params):
    _require_object_mode()
    _require_mapping(params, "params")
    name = _require_name(params)
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def cmd_set_transform(params):
    _require_mapping(params, "params")
    name = _require_name(params)
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    location = _require_vector(params["location"], "location") if "location" in params else None
    rotation = _require_vector(params["rotation"], "rotation") if "rotation" in params else None
    scale = _require_vector(params["scale"], "scale") if "scale" in params else None
    old_matrix_basis = obj.matrix_basis.copy()
    try:
        if location is not None:
            obj.location = location
        if rotation is not None:
            _apply_rotation(obj, rotation)
        if scale is not None:
            obj.scale = scale
    except BaseException:
        obj.matrix_basis = old_matrix_basis
        raise
    return _obj_summary(obj)


_BSDF_COLOR_INPUTS = {
    "base_color": "Base Color",
    "emission_color": "Emission Color",
}

_BSDF_SCALAR_INPUTS = {
    "metallic": "Metallic",
    "roughness": "Roughness",
    "emission_strength": "Emission Strength",
    "alpha": "Alpha",
    "ior": "IOR",
    "transmission": "Transmission Weight",
    "specular": "Specular IOR Level",
    "coat": "Coat Weight",
    "sheen": "Sheen Weight",
    "subsurface": "Subsurface Weight",
}

_BSDF_SCALAR_LIMITS = {
    "metallic": (0.0, 1.0),
    "roughness": (0.0, 1.0),
    "emission_strength": (0.0, 1_000_000.0),
    "alpha": (0.0, 1.0),
    "ior": (1.0, 1000.0),
    "transmission": (0.0, 1.0),
    "specular": (0.0, 1.0),
    "coat": (0.0, 1.0),
    "sheen": (0.0, 1.0),
    "subsurface": (0.0, 1.0),
}


def _bsdf_input(bsdf, input_name):
    inp = bsdf.inputs.get(input_name)
    if inp is None:
        raise ValueError(
            f"Principled BSDF has no input named '{input_name}' in this Blender version"
        )
    return inp


def cmd_create_material(params):
    _require_mapping(params, "params")
    name = _require_name(params)
    colors = {}
    for param in _BSDF_COLOR_INPUTS:
        if param in params:
            value = params[param]
            if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
                raise TypeError(f"{param} must have 3 (RGB) or 4 (RGBA) components")
            colors[param] = [
                _require_finite_number(component, f"{param}[{index}]", minimum=0.0, maximum=1.0)
                for index, component in enumerate(value)
            ]
            if len(colors[param]) == 3:
                colors[param].append(1.0)
    scalars = {}
    for param, (minimum, maximum) in _BSDF_SCALAR_LIMITS.items():
        if param in params:
            scalars[param] = _require_finite_number(
                params[param], param, minimum=minimum, maximum=maximum
            )

    requested = set(colors) | set(scalars)
    mat = bpy.data.materials.get(name)
    if mat is not None and mat.node_tree is not None and requested:
        existing_bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if existing_bsdf is None:
            raise ValueError(
                f"Material '{name}' has no 'Principled BSDF' node (custom node setup?); "
                "cannot set the requested properties"
            )
    if mat is None:
        mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        if requested:
            raise ValueError(
                f"Material '{name}' has no 'Principled BSDF' node (custom node setup?); "
                "cannot set the requested properties"
            )
        return {"material": mat.name}
    # Resolve every requested socket before changing any value. This avoids a
    # partial material update when a Blender version lacks one of the inputs.
    sockets = {
        param: _bsdf_input(bsdf, _BSDF_COLOR_INPUTS.get(param) or _BSDF_SCALAR_INPUTS[param])
        for param in requested
    }
    for param, value in colors.items():
        sockets[param].default_value = value
    for param, value in scalars.items():
        sockets[param].default_value = value
    if scalars.get("alpha", 1.0) < 1.0:
        # Opaque materials ignore alpha in EEVEE; switch to a blended mode.
        if hasattr(mat, "blend_method"):
            mat.blend_method = 'BLEND'
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'BLENDED'
    return {"material": mat.name}


def cmd_assign_material(params):
    _require_mapping(params, "params")
    object_name = _require_string(params["object_name"], "object_name")
    material_name = _require_string(params["material_name"], "material_name")
    obj = _get_scene_object(object_name)
    if obj is None:
        raise ValueError(f"No object named '{object_name}'")
    if obj.data is None or not hasattr(obj.data, "materials"):
        raise ValueError(f"Object '{obj.name}' (type {obj.type}) does not support materials")
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        raise ValueError(f"No material named '{material_name}'")
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return {"object": obj.name, "material": mat.name}


_LIGHT_TYPES = {"POINT", "SUN", "SPOT", "AREA"}


def cmd_add_light(params):
    _require_mapping(params, "params")
    light_type = _require_string(params.get("type", "POINT"), "type").upper()
    if light_type not in _LIGHT_TYPES:
        raise ValueError(f"Unknown light type '{light_type}'. Options: {sorted(_LIGHT_TYPES)}")
    name = _require_string(params.get("name", "Light"), "name")
    location = _require_vector(params.get("location", (0, 0, 5)), "location")
    color = (
        _require_vector(params["color"], "color", minimum=0.0, maximum=1.0)
        if "color" in params else None
    )
    rotation = _require_vector(params["rotation"], "rotation") if "rotation" in params else None
    energy = params.get("energy")
    if energy is None:
        # Sun strength is irradiance (W/m^2, a few units); the rest are Watts.
        energy = 3.0 if light_type == "SUN" else 1000.0
    energy = _require_finite_number(energy, "energy", minimum=0.0, maximum=1_000_000_000.0)
    light_data = bpy.data.lights.new(name=name, type=light_type)
    light_obj = None
    try:
        light_data.energy = energy
        if color is not None:
            light_data.color = color
        light_obj = bpy.data.objects.new(name=name, object_data=light_data)
        bpy.context.collection.objects.link(light_obj)
        light_obj.location = location
        if rotation is not None:
            _apply_rotation(light_obj, rotation)
    except BaseException:
        if light_obj is not None and light_obj.name in bpy.data.objects:
            bpy.data.objects.remove(light_obj, do_unlink=True)
        if light_data.name in bpy.data.lights:
            bpy.data.lights.remove(light_data)
        raise
    return _obj_summary(light_obj)


def cmd_set_camera(params):
    _require_mapping(params, "params")
    name = _require_string(params.get("name", "Camera"), "name")
    location = _require_vector(params["location"], "location") if "location" in params else None
    rotation = _require_vector(params["rotation"], "rotation") if "rotation" in params else None
    look_at = _require_vector(params["look_at"], "look_at") if "look_at" in params else None
    lens = (
        _require_finite_number(params["lens"], "lens", minimum=1.0e-6, maximum=100_000.0)
        if "lens" in params else None
    )
    make_active = _require_bool(params.get("make_active", True), "make_active")
    if rotation is not None and look_at is not None:
        raise ValueError("Pass either rotation or look_at, not both")

    cam = _get_scene_object(name)
    if cam is not None and cam.type != "CAMERA":
        raise ValueError(f"Object '{name}' exists but is type {cam.type}, not CAMERA")
    created = cam is None
    old_world = cam.matrix_world.copy() if cam is not None else None
    old_lens = cam.data.lens if cam is not None else None
    old_active_camera = bpy.context.scene.camera
    cam_data = cam.data if cam is not None else None
    try:
        if created:
            cam_data = bpy.data.cameras.new(name)
            cam = bpy.data.objects.new(name, cam_data)
            bpy.context.collection.objects.link(cam)
        if location is not None:
            cam.location = location
        if rotation is not None:
            _apply_rotation(cam, rotation)
        if look_at is not None:
            # matrix_world is stale until the depsgraph re-evaluates the
            # location set above, so force an update before reading it.
            bpy.context.view_layer.update()
            position = cam.matrix_world.translation
            direction = Vector(look_at) - position
            if direction.length < 1e-9:
                raise ValueError("look_at target coincides with the camera position")
            quat = direction.to_track_quat('-Z', 'Y')
            cam.matrix_world = Matrix.LocRotScale(position, quat, cam.matrix_world.to_scale())
        if lens is not None:
            cam.data.lens = lens
        if make_active:
            bpy.context.scene.camera = cam
    except BaseException:
        bpy.context.scene.camera = old_active_camera
        if created:
            if cam is not None and cam.name in bpy.data.objects:
                bpy.data.objects.remove(cam, do_unlink=True)
            if cam_data is not None and cam_data.name in bpy.data.cameras:
                bpy.data.cameras.remove(cam_data)
        else:
            cam.matrix_world = old_world
            cam.data.lens = old_lens
        raise
    return _obj_summary(cam)


_DEFAULT_RENDER_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_render.png")
_DEFAULT_SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_viewport.png")
_DEFAULT_THUMBNAIL_PATH = os.path.join(tempfile.gettempdir(), "blender_mcp_thumbnail.png")


def cmd_render_scene(params):
    _require_mapping(params, "params")
    scene = bpy.context.scene
    resolution_x = _require_int(
        params.get("resolution_x", 1024),
        "resolution_x",
        minimum=1,
        maximum=MAX_RENDER_DIMENSION,
    )
    resolution_y = _require_int(
        params.get("resolution_y", 1024),
        "resolution_y",
        minimum=1,
        maximum=MAX_RENDER_DIMENSION,
    )
    if resolution_x * resolution_y > MAX_RENDER_PIXELS:
        raise ValueError(f"render size must not exceed {MAX_RENDER_PIXELS} total pixels")
    samples = _require_int(
        params.get("samples", 64), "samples", minimum=1, maximum=MAX_RENDER_SAMPLES
    )
    filepath_param = params.get("filepath") or _DEFAULT_RENDER_PATH
    filepath_param = _require_string(filepath_param, "filepath", max_length=4096)
    return_image = _require_bool(params.get("return_image", True), "return_image")
    if scene.camera is None:
        raise RuntimeError("Scene has no active camera; use set_camera first")

    render = scene.render
    engine = render.engine
    use_cycles = engine == "CYCLES" and hasattr(scene, "cycles")
    use_eevee = engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT") and hasattr(scene, "eevee")
    saved = {
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "filepath": render.filepath,
        "file_format": render.image_settings.file_format,
        "use_file_extension": render.use_file_extension,
        "samples": scene.cycles.samples if use_cycles
        else scene.eevee.taa_render_samples if use_eevee else None,
    }

    filepath = bpy.path.abspath(filepath_param)
    try:
        render.resolution_x = resolution_x
        render.resolution_y = resolution_y
        render.resolution_percentage = 100
        render.filepath = filepath
        render.image_settings.file_format = "PNG"
        render.use_file_extension = False
        if use_cycles:
            scene.cycles.samples = samples
        elif use_eevee:
            scene.eevee.taa_render_samples = samples
        render_result = bpy.ops.render.render(write_still=True)
        if 'FINISHED' not in render_result:
            raise RuntimeError(f"Render did not complete (result: {render_result!r})")
    finally:
        # Don't leave MCP render settings behind on the user's scene.
        render.resolution_x = saved["resolution_x"]
        render.resolution_y = saved["resolution_y"]
        render.resolution_percentage = saved["resolution_percentage"]
        render.filepath = saved["filepath"]
        render.image_settings.file_format = saved["file_format"]
        render.use_file_extension = saved["use_file_extension"]
        if saved["samples"] is not None:
            if use_cycles:
                scene.cycles.samples = saved["samples"]
            elif use_eevee:
                scene.eevee.taa_render_samples = saved["samples"]

    result = {"filepath": filepath}
    if return_image:
        with open(filepath, "rb") as f:
            result["image_base64"] = base64.b64encode(f.read()).decode("ascii")
    return result


def cmd_get_viewport_screenshot(params):
    _require_mapping(params, "params")
    filepath_param = _require_string(
        params.get("filepath") or _DEFAULT_SCREENSHOT_PATH, "filepath", max_length=4096
    )
    filepath = bpy.path.abspath(filepath_param)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                with bpy.context.temp_override(window=window, area=area):
                    shot_result = bpy.ops.screen.screenshot_area(filepath=filepath)
                if 'FINISHED' not in shot_result:
                    raise RuntimeError(f"Screenshot did not complete (result: {shot_result!r})")
                with open(filepath, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
                return {"filepath": filepath, "image_base64": data}
    raise RuntimeError("No VIEW_3D area found to screenshot")


def _save_selection():
    """Snapshot selection by name; joins/deletes can invalidate object refs."""
    active = bpy.context.view_layer.objects.active
    return {
        "active": active.name if active else None,
        "selected": [o.name for o in bpy.context.selected_objects],
    }


def _restore_selection(state):
    bpy.context.view_layer.update()
    view_objects = bpy.context.view_layer.objects
    for obj in view_objects:
        if obj is not None:
            obj.select_set(False)
    for name in state["selected"]:
        obj = view_objects.get(name)
        if obj is not None:
            obj.select_set(True)
    active = view_objects.get(state["active"]) if state["active"] else None
    bpy.context.view_layer.objects.active = active


def _capsule_point(center, radial_x, radial_y, axis, radius_at_ring, angle, z):
    return (
        center
        + radial_x * (radius_at_ring * math.cos(angle))
        + radial_y * (radius_at_ring * math.sin(angle))
        + axis * z
    )


def _build_capsule_mesh(name, start, end, radius, caps, *, segments=32, hemisphere_rings=8):
    """Build one connected capsule surface directly in world coordinates."""
    direction = end - start
    length = direction.length
    axis = direction / length
    center = (start + end) / 2.0
    half_length = length / 2.0

    reference = Vector((0.0, 0.0, 1.0)) if abs(axis.z) < 0.999 else Vector((1.0, 0.0, 0.0))
    radial_x = axis.cross(reference).normalized()
    radial_y = axis.cross(radial_x).normalized()

    mesh = bpy.data.meshes.new(name)
    obj = None
    bm = bmesh.new()
    try:
        def make_ring(ring_radius, z):
            return [
                bm.verts.new(
                    _capsule_point(
                        center,
                        radial_x,
                        radial_y,
                        axis,
                        ring_radius,
                        2.0 * math.pi * index / segments,
                        z,
                    )
                )
                for index in range(segments)
            ]

        if caps:
            bottom_pole = bm.verts.new(center + axis * (-half_length - radius))
            rings = []
            for step in range(1, hemisphere_rings + 1):
                latitude = -math.pi / 2.0 + step * math.pi / (2.0 * hemisphere_rings)
                rings.append(make_ring(radius * math.cos(latitude), -half_length + radius * math.sin(latitude)))
            rings.append(make_ring(radius, half_length))
            for step in range(1, hemisphere_rings):
                latitude = step * math.pi / (2.0 * hemisphere_rings)
                rings.append(make_ring(radius * math.cos(latitude), half_length + radius * math.sin(latitude)))
            top_pole = bm.verts.new(center + axis * (half_length + radius))

            first_ring = rings[0]
            for index in range(segments):
                next_index = (index + 1) % segments
                bm.faces.new((bottom_pole, first_ring[next_index], first_ring[index]))
        else:
            rings = [make_ring(radius, -half_length), make_ring(radius, half_length)]
            bm.faces.new(tuple(reversed(rings[0])))

        for lower, upper in zip(rings, rings[1:]):
            for index in range(segments):
                next_index = (index + 1) % segments
                bm.faces.new((lower[index], lower[next_index], upper[next_index], upper[index]))

        if caps:
            last_ring = rings[-1]
            for index in range(segments):
                next_index = (index + 1) % segments
                bm.faces.new((last_ring[index], last_ring[next_index], top_pole))
        else:
            bm.faces.new(tuple(rings[-1]))

        bm.normal_update()
        bm.to_mesh(mesh)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        return obj
    except BaseException:
        if obj is not None and obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        raise
    finally:
        bm.free()


def cmd_add_capsule(params):
    _require_object_mode()
    _require_mapping(params, "params")
    start = Vector(_require_vector(params["start"], "start"))
    end = Vector(_require_vector(params["end"], "end"))
    radius = _require_finite_number(
        params.get("radius", 0.1), "radius", minimum=1.0e-9, maximum=1_000_000.0
    )
    name = _require_string(params.get("name") or "Capsule", "name")
    caps = _require_bool(params.get("caps", True), "caps")
    if (end - start).length < 1.0e-6:
        raise ValueError("start and end must differ by at least 1e-6")

    obj = _build_capsule_mesh(name, start, end, radius, caps)
    return _obj_summary(obj)


def cmd_mirror_object(params):
    _require_object_mode()
    _require_mapping(params, "params")
    name = _require_name(params)
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    axis = _require_string(params.get("axis", "X"), "axis").upper()
    axis_index = {"X": 0, "Y": 1, "Z": 2}.get(axis)
    if axis_index is None:
        raise ValueError(f"axis must be one of X, Y, Z, got '{axis}'")
    new_name = (
        _require_string(params["new_name"], "new_name")
        if params.get("new_name") is not None else f"{name}_mirror"
    )

    new_obj = obj.copy()
    try:
        if obj.data is not None:
            new_obj.data = obj.data.copy()
        new_obj.name = new_name
        bpy.context.collection.objects.link(new_obj)

        # Reflect the full world transform across the world-origin plane
        # perpendicular to the axis - exactly what Blender's own Object > Mirror
        # does. The resulting negative-determinant transform renders correctly
        # as-is, so the mesh data must NOT be normal-flipped here; join_objects
        # corrects winding if the mirrored copy is later baked in.
        reflect = Matrix.Identity(4)
        reflect[axis_index][axis_index] = -1.0
        new_obj.matrix_world = reflect @ obj.matrix_world
    except BaseException:
        copied_data = new_obj.data if new_obj.data is not obj.data else None
        if new_obj.name in bpy.data.objects:
            bpy.data.objects.remove(new_obj, do_unlink=True)
        if copied_data is not None and copied_data.users == 0:
            bpy.data.batch_remove(ids=(copied_data,))
        raise
    return _obj_summary(new_obj)


def cmd_parent_object(params):
    _require_mapping(params, "params")
    child_name = _require_string(params["child"], "child")
    parent_name = _require_string(params["parent"], "parent")
    keep_transform = _require_bool(params.get("keep_transform", True), "keep_transform")
    child = _get_scene_object(child_name)
    if child is None:
        raise ValueError(f"No object named '{child_name}'")
    parent = _get_scene_object(parent_name)
    if parent is None:
        raise ValueError(f"No object named '{parent_name}'")
    ancestor = parent
    while ancestor is not None:
        if ancestor is child:
            raise ValueError("parenting would create a dependency cycle")
        ancestor = ancestor.parent

    old_parent = child.parent
    old_parent_inverse = child.matrix_parent_inverse.copy()
    old_world = child.matrix_world.copy()
    try:
        child.parent = parent
        child.matrix_parent_inverse = Matrix.Identity(4)
        if keep_transform:
            child.matrix_world = old_world
    except BaseException:
        child.parent = old_parent
        child.matrix_parent_inverse = old_parent_inverse
        child.matrix_world = old_world
        raise
    return {"child": child.name, "parent": parent.name}


def _flip_winding(mesh):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        for f in bm.faces:
            f.normal_flip()
        bm.normal_update()
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()


def cmd_join_objects(params):
    _require_object_mode()
    _require_mapping(params, "params")
    names_value = params["names"]
    if not isinstance(names_value, (list, tuple)):
        raise TypeError("names must be a list of object names")
    if len(names_value) > MAX_JOIN_OBJECTS:
        raise ValueError(f"names must contain at most {MAX_JOIN_OBJECTS} objects")
    names = [_require_string(name, f"names[{index}]") for index, name in enumerate(names_value)]
    if len(set(names)) != len(names):
        raise ValueError("names must not contain duplicates")
    target_name = (
        _require_string(params["target_name"], "target_name")
        if params.get("target_name") is not None else None
    )
    objs = []
    view_objects = bpy.context.view_layer.objects
    for n in names:
        obj = _get_scene_object(n)
        if obj is None:
            raise ValueError(f"No object named '{n}'")
        if obj.type != "MESH":
            raise ValueError(f"Object '{n}' is type {obj.type}; join_objects only supports MESH objects")
        if view_objects.get(obj.name) is None:
            raise ValueError(f"Object '{n}' is not in the active view layer")
        objs.append(obj)
    if len(objs) < 2:
        raise ValueError("Need at least 2 objects to join")
    active = objs[0]

    # Joining bakes each source's transform relative to the active object
    # into the mesh data. A mirrored (negative-determinant) relative
    # transform reverses face winding, which Blender's join does NOT
    # correct, so pre-flip those sources to keep their normals outward.
    flipped = []
    copied_meshes = []
    prev = _save_selection()
    try:
        try:
            active_inv = active.matrix_world.inverted()
        except ValueError as exc:
            raise ValueError(f"Active object '{active.name}' has a singular world transform") from exc
        for obj in objs[1:]:
            if (active_inv @ obj.matrix_world).determinant() < 0:
                if obj.data.users > 1:
                    original_mesh = obj.data
                    copied_mesh = original_mesh.copy()
                    obj.data = copied_mesh
                    copied_meshes.append((obj, original_mesh, copied_mesh))
                _flip_winding(obj.data)
                flipped.append(obj)
        for view_obj in view_objects:
            view_obj.select_set(False)
        for o in objs:
            o.select_set(True)
        if set(bpy.context.selected_objects) != set(objs):
            raise RuntimeError("Could not select exactly the requested objects for joining")
        bpy.context.view_layer.objects.active = active
        consumed_meshes = []
        for obj in objs[1:]:
            if not any(obj.data is mesh for mesh in consumed_meshes):
                consumed_meshes.append(obj.data)
        join_result = bpy.ops.object.join()
        if 'FINISHED' not in join_result:
            raise RuntimeError(f"Join did not complete (result: {join_result!r})")
    except BaseException:
        for obj in flipped:  # join failed, sources still exist: un-flip them
            uses_temporary_copy = any(obj is copied[0] for copied in copied_meshes)
            if not uses_temporary_copy and obj.name in bpy.data.objects:
                _flip_winding(obj.data)
        for obj, original_mesh, copied_mesh in copied_meshes:
            if obj.name in bpy.data.objects:
                obj.data = original_mesh
            if copied_mesh.users == 0:
                bpy.data.meshes.remove(copied_mesh)
        _restore_selection(prev)
        raise
    joined = bpy.context.active_object
    if target_name:
        joined.name = target_name
    for mesh in consumed_meshes:
        if mesh is not joined.data and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    return _obj_summary(joined)


def cmd_set_shading(params):
    _require_object_mode()
    _require_mapping(params, "params")
    name = _require_name(params)
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    if obj.type != "MESH":
        raise ValueError(f"Object '{name}' is type {obj.type}; set_shading only supports MESH objects")
    smooth = _require_bool(params.get("smooth", True), "smooth")
    for poly in obj.data.polygons:
        poly.use_smooth = smooth
    obj.data.update()
    return {"object": obj.name, "smooth": smooth, "polygons": len(obj.data.polygons)}


def _run_undo_steps(op, steps):
    steps = _require_int(steps, "steps", minimum=1, maximum=MAX_UNDO_STEPS)
    performed = 0
    for _ in range(steps):
        try:
            result = op()
        except RuntimeError:
            break
        if 'FINISHED' not in result:
            break
        performed += 1
    return performed


def cmd_undo(params):
    _require_mapping(params, "params")
    steps = params.get("steps", 1)
    performed = _run_undo_steps(bpy.ops.ed.undo, steps)
    return {"steps_requested": steps, "steps_performed": performed}


def cmd_redo(params):
    _require_mapping(params, "params")
    steps = params.get("steps", 1)
    performed = _run_undo_steps(bpy.ops.ed.redo, steps)
    return {"steps_requested": steps, "steps_performed": performed}


def cmd_execute_code(params):
    _require_mapping(params, "params")
    code = _require_string(
        params["code"], "code", allow_empty=True, max_length=MAX_EXECUTE_CODE_BYTES
    )
    if len(code.encode("utf-8")) > MAX_EXECUTE_CODE_BYTES:
        raise ValueError(f"code must be at most {MAX_EXECUTE_CODE_BYTES} UTF-8 bytes")
    local_ns = {
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        "Euler": Euler,
        "Matrix": Matrix,
        "Quaternion": Quaternion,
        "Vector": Vector,
    }
    exec(compile(code, "<mcp_execute_code>", "exec"), local_ns, local_ns)
    result = local_ns.get("result")
    try:
        json.dumps(result)
    except (TypeError, ValueError):
        result = repr(result)
    return {"result": result}


def cmd_save_file(params):
    _require_mapping(params, "params")
    filepath = params.get("filepath")
    if "filepath" in params and filepath is not None:
        filepath = _require_string(filepath, "filepath", max_length=4096)
        result = bpy.ops.wm.save_as_mainfile(filepath=bpy.path.abspath(filepath))
    elif bpy.data.filepath:
        result = bpy.ops.wm.save_mainfile()
    else:
        raise ValueError("This file has never been saved; pass an explicit filepath")
    if 'FINISHED' not in result:
        raise RuntimeError(f"Blender did not save the file (result: {result!r})")
    return {"saved": bpy.data.filepath}


# ---------------------------------------------------------------------------
# Introspection / analysis commands (all read-only; none push undo steps)
# ---------------------------------------------------------------------------

def cmd_get_objects_summary(params):
    _require_mapping(params, "params")

    def collection_summary(collection, visited):
        if collection.name in visited:
            # Blender doesn't normally allow collection cycles, but don't
            # let a corrupt file recurse forever.
            return {"name": collection.name, "recursive": True}
        visited = visited | {collection.name}
        return {
            "name": collection.name,
            "objects": [obj.name for obj in collection.objects],
            "children": [collection_summary(child, visited) for child in collection.children],
        }

    scene = bpy.context.scene
    return {
        "scene_name": scene.name,
        "collection": collection_summary(scene.collection, frozenset()),
    }


def cmd_get_window_summary(params):
    _require_mapping(params, "params")
    windows = []
    for window in bpy.context.window_manager.windows:
        areas = [
            {
                "type": area.type,
                "x": area.x,
                "y": area.y,
                "width": area.width,
                "height": area.height,
            }
            for area in window.screen.areas
        ]
        windows.append({
            "workspace": window.workspace.name if window.workspace else None,
            "screen": window.screen.name if window.screen else None,
            "areas": areas,
        })
    view_layer = bpy.context.view_layer
    return {
        "windows": windows,
        "mode": bpy.context.mode,
        "active_object": view_layer.objects.active.name if view_layer.objects.active else None,
        "selected_objects": [o.name for o in bpy.context.selected_objects],
    }


def cmd_jump_to_view3d_object(params):
    _require_mapping(params, "params")
    name = _require_name(params)
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    for obj_iter in bpy.context.view_layer.objects:
        obj_iter.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.view3d.view_selected()
                return {"focused": name}
    raise RuntimeError("No VIEW_3D area found to focus")


def cmd_render_thumbnail(params):
    _require_mapping(params, "params")
    scene = bpy.context.scene
    size = _require_int(params.get("size", 128), "size", minimum=1, maximum=MAX_THUMBNAIL_DIMENSION)
    filepath_param = params.get("filepath") or _DEFAULT_THUMBNAIL_PATH
    filepath_param = _require_string(filepath_param, "filepath", max_length=4096)
    return_image = _require_bool(params.get("return_image", True), "return_image")
    if scene.camera is None:
        raise RuntimeError("Scene has no active camera; use set_camera first")

    render = scene.render
    saved = {
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "filepath": render.filepath,
        "file_format": render.image_settings.file_format,
        "use_file_extension": render.use_file_extension,
        "engine": render.engine,
    }
    filepath = bpy.path.abspath(filepath_param)
    try:
        render.resolution_x = size
        render.resolution_y = size
        render.resolution_percentage = 100
        render.filepath = filepath
        render.image_settings.file_format = "PNG"
        render.use_file_extension = False
        try:
            # Thumbnails favor speed over fidelity; Workbench is available on
            # every build and skips ray tracing / sample convergence entirely.
            render.engine = "BLENDER_WORKBENCH"
        except TypeError:
            pass
        render_result = bpy.ops.render.render(write_still=True)
        if 'FINISHED' not in render_result:
            raise RuntimeError(f"Render did not complete (result: {render_result!r})")
    finally:
        render.resolution_x = saved["resolution_x"]
        render.resolution_y = saved["resolution_y"]
        render.resolution_percentage = saved["resolution_percentage"]
        render.filepath = saved["filepath"]
        render.image_settings.file_format = saved["file_format"]
        render.use_file_extension = saved["use_file_extension"]
        render.engine = saved["engine"]

    result = {"filepath": filepath}
    if return_image:
        with open(filepath, "rb") as f:
            result["image_base64"] = base64.b64encode(f.read()).decode("ascii")
    return result


def cmd_get_blendfile_summary_datablocks(params):
    _require_mapping(params, "params")
    counts = {}
    for attr in dir(bpy.data):
        if attr.startswith("_"):
            continue
        collection = getattr(bpy.data, attr, None)
        if isinstance(collection, bpy.types.bpy_prop_collection):
            counts[attr] = len(collection)
    return {
        "datablock_counts": counts,
        "active_workspace": bpy.context.workspace.name if bpy.context.workspace else None,
        "render_engine": bpy.context.scene.render.engine,
    }


_MISSING_FILE_CATEGORIES = (
    "images", "libraries", "fonts", "sounds", "movieclips", "cache_files",
)


def cmd_get_blendfile_summary_missing_files(params):
    _require_mapping(params, "params")
    missing = []
    for category in _MISSING_FILE_CATEGORIES:
        collection = getattr(bpy.data, category, None)
        if collection is None:
            continue
        for block in collection:
            filepath = getattr(block, "filepath", "") or ""
            if not filepath or filepath.startswith("<"):
                continue
            # Packed data-blocks embed their bytes in the .blend file itself,
            # so a missing on-disk path is expected, not an error.
            if getattr(block, "packed_file", None) is not None:
                continue
            if not os.path.exists(bpy.path.abspath(filepath)):
                missing.append({"category": category, "name": block.name, "filepath": filepath})
    return {"missing_count": len(missing), "missing": missing}


def cmd_get_blendfile_summary_linked_libraries(params):
    _require_mapping(params, "params")
    libraries = list(bpy.data.libraries)
    by_name = {lib.name: lib for lib in libraries}
    children = {lib.name: [] for lib in libraries}
    roots = []
    for lib in libraries:
        parent = lib.parent
        if parent is not None and parent.name in children:
            children[parent.name].append(lib.name)
        else:
            roots.append(lib.name)

    def build(name, visited):
        if name in visited:
            return {"name": name, "recursive": True}
        visited = visited | {name}
        lib = by_name[name]
        return {
            "name": lib.name,
            "filepath": lib.filepath,
            "children": [build(child, visited) for child in children[name]],
        }

    return {"libraries": [build(name, frozenset()) for name in roots]}


def cmd_get_blendfile_summary_path_info(params):
    _require_mapping(params, "params")
    filepath = bpy.data.filepath
    is_saved = bool(filepath)
    result = {
        "filepath": filepath or None,
        "is_saved": is_saved,
        "is_dirty": bool(bpy.data.is_dirty),
    }
    if is_saved:
        abs_path = bpy.path.abspath(filepath)
        if os.path.exists(abs_path):
            stat = os.stat(abs_path)
            result["file_size_bytes"] = stat.st_size
            result["seconds_since_saved"] = max(0.0, time.time() - stat.st_mtime)
        backup_dir = os.path.dirname(abs_path) or "."
        base_name = os.path.basename(abs_path)
        try:
            result["backup_count"] = sum(
                1 for entry in os.listdir(backup_dir)
                if entry != base_name and entry.startswith(base_name)
            )
        except OSError:
            result["backup_count"] = 0
    return result


def cmd_get_blendfile_summary_usage_guess(params):
    _require_mapping(params, "params")
    scene = bpy.context.scene
    signals = []

    def add(label, score, reason):
        signals.append({"label": label, "score": min(100, score), "reason": reason})

    armature_count = len(bpy.data.armatures)
    if armature_count:
        add("character_rigging", 40 + armature_count * 20,
            f"{armature_count} armature data-block(s) present")

    geo_node_group_count = sum(1 for g in bpy.data.node_groups if g.type == "GEOMETRY")
    if geo_node_group_count:
        add("procedural_geometry_nodes", 30 + geo_node_group_count * 15,
            f"{geo_node_group_count} geometry-node group(s) present")

    sequence_editor = scene.sequence_editor
    # Blender 5.0 renamed VSE `sequences` to `strips_all`; support both so this
    # works across the addon's declared minimum (4.0) and current versions.
    strips = getattr(sequence_editor, "strips_all", None)
    if strips is None:
        strips = getattr(sequence_editor, "sequences", None)
    strip_count = len(strips) if strips is not None else 0
    if strip_count:
        add("video_editing", 50 + strip_count * 5, f"{strip_count} sequencer strip(s) present")

    compositor_tree = getattr(scene, "node_tree", None)
    node_count = len(compositor_tree.nodes) if scene.use_nodes and compositor_tree else 0
    if node_count > 2:
        add("compositing", 30 + node_count * 3, f"{node_count} compositor node(s) present")

    grease_pencil_count = len(bpy.data.grease_pencils)
    if grease_pencil_count:
        add("2d_animation_grease_pencil", 40 + grease_pencil_count * 20,
            f"{grease_pencil_count} grease pencil data-block(s) present")

    material_count = len(bpy.data.materials)
    mesh_count = len(bpy.data.meshes)
    if material_count and mesh_count and not armature_count and not geo_node_group_count:
        add("static_asset_lookdev", 20 + material_count * 5,
            f"{material_count} material(s) on {mesh_count} mesh(es), "
            "no rigging or procedural setup detected")

    signals.sort(key=lambda s: s["score"], reverse=True)
    return {"guesses": signals}


def _resolve_api_identifier(path):
    parts = path.split(".")
    if not parts or parts[0] != "bpy":
        raise ValueError("identifier must start with 'bpy'")
    target = bpy
    for part in parts[1:]:
        target = getattr(target, part)
    return target


def cmd_get_python_api_docs(params):
    _require_mapping(params, "params")
    identifier = _require_string(params["identifier"], "identifier", max_length=256)

    if identifier.endswith("*"):
        container_path, _, name_prefix = identifier[:-1].rpartition(".")
        if not container_path:
            raise ValueError("wildcard identifiers must look like 'bpy.types.Mesh*'")
        try:
            container = _resolve_api_identifier(container_path)
        except AttributeError as exc:
            raise ValueError(f"Unknown identifier '{container_path}': {exc}") from None
        matches = sorted(
            name for name in dir(container)
            if not name.startswith("_") and name.startswith(name_prefix)
        )
        return {"identifier": identifier, "matches": matches[:MAX_API_DOCS_MATCHES]}

    try:
        target = _resolve_api_identifier(identifier)
    except AttributeError:
        # RNA properties (e.g. bpy.types.Object.location) are exposed through
        # bl_rna.properties, not as plain Python class attributes; fall back
        # to that lookup before giving up.
        parent_path, _, prop_name = identifier.rpartition(".")
        parent = None
        if parent_path:
            try:
                parent = _resolve_api_identifier(parent_path)
            except AttributeError:
                parent = None
        bl_rna = getattr(parent, "bl_rna", None) if parent is not None else None
        prop = bl_rna.properties.get(prop_name) if bl_rna is not None else None
        if prop is None:
            raise ValueError(f"Unknown identifier '{identifier}'") from None
        return {
            "identifier": identifier,
            "doc": prop.description,
            "type": prop.type,
        }

    result = {"identifier": identifier, "doc": (target.__doc__ or "").strip()}
    bl_rna = getattr(target, "bl_rna", None)
    if bl_rna is not None:
        result["properties"] = [
            {"name": prop.identifier, "type": prop.type, "description": prop.description}
            for prop in bl_rna.properties
            if prop.identifier != "rna_type"
        ]
        functions = getattr(bl_rna, "functions", None)
        if functions:
            result["functions"] = [f.identifier for f in functions]
    elif callable(target):
        try:
            result["signature"] = str(inspect.signature(target))
        except (TypeError, ValueError):
            pass
    return result


_MUTATING_COMMANDS = {
    "add_primitive", "delete_object", "set_transform", "create_material",
    "assign_material", "add_light", "set_camera", "add_capsule",
    "mirror_object", "parent_object", "join_objects", "set_shading",
    "execute_code",
}


_HANDLERS = {
    "get_scene_info": cmd_get_scene_info,
    "get_object_info": cmd_get_object_info,
    "add_primitive": cmd_add_primitive,
    "delete_object": cmd_delete_object,
    "set_transform": cmd_set_transform,
    "create_material": cmd_create_material,
    "assign_material": cmd_assign_material,
    "add_light": cmd_add_light,
    "set_camera": cmd_set_camera,
    "render_scene": cmd_render_scene,
    "get_viewport_screenshot": cmd_get_viewport_screenshot,
    "add_capsule": cmd_add_capsule,
    "mirror_object": cmd_mirror_object,
    "parent_object": cmd_parent_object,
    "join_objects": cmd_join_objects,
    "set_shading": cmd_set_shading,
    "undo": cmd_undo,
    "redo": cmd_redo,
    "execute_code": cmd_execute_code,
    "save_file": cmd_save_file,
    "get_objects_summary": cmd_get_objects_summary,
    "get_window_summary": cmd_get_window_summary,
    "jump_to_view3d_object": cmd_jump_to_view3d_object,
    "render_thumbnail": cmd_render_thumbnail,
    "get_blendfile_summary_datablocks": cmd_get_blendfile_summary_datablocks,
    "get_blendfile_summary_missing_files": cmd_get_blendfile_summary_missing_files,
    "get_blendfile_summary_linked_libraries": cmd_get_blendfile_summary_linked_libraries,
    "get_blendfile_summary_path_info": cmd_get_blendfile_summary_path_info,
    "get_blendfile_summary_usage_guess": cmd_get_blendfile_summary_usage_guess,
    "get_python_api_docs": cmd_get_python_api_docs,
}


# Commands permitted from blender_mcp_cli_runner.py, which runs a single
# command against a .blend file opened headless (`blender --background
# <file>`) with no live interactive session and nobody watching. Limited to
# handlers that only read bpy.data (no window/viewport dependency) plus the
# execute_code escape hatch, matching what actually works headlessly.
CLI_SAFE_COMMANDS = frozenset({
    "get_blendfile_summary_datablocks",
    "get_blendfile_summary_missing_files",
    "get_blendfile_summary_linked_libraries",
    "get_blendfile_summary_path_info",
    "get_blendfile_summary_usage_guess",
    "execute_code",
})


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

def _command_fingerprint(command):
    canonical = json.dumps(
        {"type": command["type"], "params": command["params"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _prune_response_cache(now=None):
    global _response_cache_bytes
    now = time.monotonic() if now is None else now
    while _response_cache:
        _request_id, (created_at, _fingerprint, _response, size) = next(
            iter(_response_cache.items())
        )
        if now - created_at <= DEDUP_TTL_SECONDS:
            break
        _response_cache.popitem(last=False)
        _response_cache_bytes -= size


def _get_cached_response(request_id, fingerprint):
    _prune_response_cache()
    cached = _response_cache.get(request_id)
    if cached is None:
        return None
    created_at, cached_fingerprint, payload, size = cached
    if cached_fingerprint != fingerprint:
        raise ValueError("request ID was already used for a different command")
    _response_cache.move_to_end(request_id)
    _response_cache[request_id] = (time.monotonic(), cached_fingerprint, payload, size)
    return json.loads(payload)


def _store_cached_response(request_id, fingerprint, payload, size):
    global _response_cache_bytes
    previous = _response_cache.pop(request_id, None)
    if previous is not None:
        _response_cache_bytes -= previous[3]
    _response_cache[request_id] = (time.monotonic(), fingerprint, payload, size)
    _response_cache_bytes += size
    _prune_response_cache()
    while (
        len(_response_cache) > MAX_DEDUP_ENTRIES
        or _response_cache_bytes > MAX_DEDUP_CACHE_BYTES
    ):
        _request_id, (_created_at, _fingerprint, _response, removed_size) = (
            _response_cache.popitem(last=False)
        )
        _response_cache_bytes -= removed_size


def _safe_exception_text(exc):
    try:
        text = str(exc)
    except BaseException:
        text = ""
    if text:
        return text
    try:
        return type(exc).__name__
    except BaseException:
        return "Unhandled BaseException"


def _safe_traceback():
    try:
        return traceback.format_exc()
    except BaseException:
        return "Traceback unavailable because exception formatting failed"


def _safe_response_id(response):
    if not isinstance(response, dict):
        return None
    request_id = response.get("id")
    if not isinstance(request_id, str):
        return None
    try:
        encoded = request_id.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        return None
    return request_id if len(encoded) <= MAX_REQUEST_ID_BYTES else None


def _bounded_response(response):
    """Return a serializable, size-bounded response and its JSON payload."""
    request_id = _safe_response_id(response)
    try:
        payload = json.dumps(response, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        response = {
            "id": request_id,
            "status": "error",
            "error": f"Result not JSON-serializable: {_safe_exception_text(exc)}",
        }
        payload = json.dumps(response, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RESPONSE_BYTES:
        response = {
            "id": request_id,
            "status": "error",
            "error": f"Response exceeds the {MAX_RESPONSE_BYTES}-byte limit",
        }
        payload = json.dumps(response, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RESPONSE_BYTES:
        # Defensive terminal fallback for tests or deployments configured
        # below the normal small error-response size.
        response = {"id": None, "status": "error", "error": "Response too large"}
        payload = json.dumps(response, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RESPONSE_BYTES:
        response = {}
        payload = "{}"
    return response, payload


def _execute_queued_command(command, command_generation):
    request_id = None
    fingerprint = None
    cacheable = False
    command_type = None
    handler_started = False
    handler_succeeded = False
    try:
        _require_mapping(command, "command")
        request_id = _require_string(
            command.get("id"), "request id", max_length=MAX_REQUEST_ID_BYTES
        )
        if len(request_id.encode("utf-8")) > MAX_REQUEST_ID_BYTES:
            raise ValueError(f"request id must be at most {MAX_REQUEST_ID_BYTES} UTF-8 bytes")
        if command_generation != _generation or not _running:
            raise RuntimeError("Command belongs to a stopped server generation")
        command_type = _require_string(command.get("type"), "command type", max_length=128)
        command_params = _require_mapping(command.get("params", {}), "params")
        command = {"id": request_id, "type": command_type, "params": command_params}
        fingerprint = _command_fingerprint(command)
        cached_response = _get_cached_response(request_id, fingerprint)
        if cached_response is not None:
            return cached_response
        cacheable = True

        handler = _HANDLERS.get(command_type)
        if handler is None:
            raise ValueError(f"Unknown command type '{command_type}'")
        handler_started = True
        data = handler(command_params)
        handler_succeeded = True
        response = {"id": request_id, "status": "ok", "result": data}
    except BaseException as exc:
        # Arbitrary execute_code input can raise SystemExit, KeyboardInterrupt,
        # a hostile __str__, or a custom BaseException. None may escape a
        # Blender timer, because Blender permanently unregisters a timer that
        # raises.
        response = {
            "id": request_id,
            "status": "error",
            "error": _safe_exception_text(exc),
            "traceback": _safe_traceback(),
        }

    should_push_undo = (
        command_type in _MUTATING_COMMANDS and handler_succeeded
    ) or (command_type == "execute_code" and handler_started)
    if should_push_undo:
        undo_error = None
        try:
            undo_result = bpy.ops.ed.undo_push(message=f"MCP: {command_type}")
            if 'FINISHED' not in undo_result:
                undo_error = f"undo_push returned {undo_result!r}"
        except BaseException as exc:
            undo_error = _safe_exception_text(exc)
        if undo_error is not None:
            if response.get("status") == "ok":
                response = {
                    "id": request_id,
                    "status": "error",
                    "error": (
                        "Command completed, but Blender could not create its undo boundary: "
                        f"{undo_error}"
                    ),
                }
            else:
                response["error"] = (
                    f"{response.get('error', 'Command failed')}; additionally, Blender could "
                    f"not create an undo boundary: {undo_error}"
                )

    response, payload = _bounded_response(response)
    if cacheable:
        _store_cached_response(
            request_id, fingerprint, payload, len(payload.encode("utf-8"))
        )
    return response


def _process_queue():
    """Runs on Blender's main thread via bpy.app.timers."""
    processed = 0
    while processed < 20:
        try:
            command, response_box, command_generation = _command_queue.get_nowait()
        except queue.Empty:
            break
        processed += 1
        try:
            response = _execute_queued_command(command, command_generation)
        except BaseException as exc:
            request_id = _safe_response_id(
                {"id": command.get("id") if isinstance(command, dict) else None}
            )
            response = {
                "id": request_id,
                "status": "error",
                "error": f"Internal command-processing failure: {_safe_exception_text(exc)}",
                "traceback": _safe_traceback(),
            }
            try:
                response, _payload = _bounded_response(response)
            except BaseException:
                response = {"id": None, "status": "error", "error": "Internal failure"}
        try:
            response_box.put_nowait(response)
        except BaseException:
            # A broken response queue must not unregister Blender's timer. The
            # waiting client will observe its normal connection/timeout error.
            pass
    # Drain fast while commands are waiting, idle politely otherwise. Both
    # rates are user-configurable (preferences panel) to trade latency for
    # idle CPU overhead; fall back to the historical defaults if the scene
    # property isn't available yet (e.g. during registration or in tests).
    active_interval, idle_interval = 0.0, 0.05
    try:
        settings = bpy.context.scene.blender_mcp_settings
        active_interval = max(0.0, float(settings.poll_interval_active))
        idle_interval = max(0.0, float(settings.poll_interval_idle))
    except BaseException:
        pass
    return active_interval if not _command_queue.empty() else idle_interval


def _send_response(conn, response):
    _response, payload = _bounded_response(response)
    conn.sendall((payload + "\n").encode("utf-8"))


def _handle_client(conn, generation):
    buf = b""
    try:
        while _running and generation == _generation:
            try:
                chunk = conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            newline_index = buf.find(b"\n")
            if (
                newline_index > MAX_LINE_BYTES
                or (newline_index < 0 and len(buf) > MAX_LINE_BYTES)
            ):
                try:
                    _send_response(
                        conn,
                        {"id": None, "status": "error", "error": "Command exceeds max size"},
                    )
                except OSError:
                    pass
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    command = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    try:
                        _send_response(
                            conn,
                            {"id": None, "status": "error", "error": f"bad json: {exc}"},
                        )
                    except OSError:
                        pass
                    continue
                response_box = queue.Queue()
                try:
                    _command_queue.put_nowait((command, response_box, generation))
                except queue.Full:
                    request_id = command.get("id") if isinstance(command, dict) else None
                    try:
                        _send_response(
                            conn,
                            {
                                "id": request_id,
                                "status": "error",
                                "error": f"Command queue is full ({MAX_PENDING_COMMANDS} pending)",
                            },
                        )
                    except OSError:
                        return
                    continue
                response = None
                while _running and generation == _generation:
                    try:
                        response = response_box.get(timeout=0.5)
                        break
                    except queue.Empty:
                        continue
                if response is None:
                    request_id = command.get("id") if isinstance(command, dict) else None
                    response = {"id": request_id, "status": "error", "error": "Server is stopping"}
                try:
                    _send_response(conn, response)
                except OSError:
                    break
    except (ConnectionResetError, OSError):
        pass
    finally:
        with _client_sockets_lock:
            _client_sockets.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass


def _close_client_sockets(generation=None):
    """Close all tracked clients, or only clients from one listener generation."""
    with _client_sockets_lock:
        stale_sockets = [
            client_sock
            for client_sock, client_generation in _client_sockets.items()
            if generation is None or client_generation == generation
        ]
        for client_sock in stale_sockets:
            _client_sockets.pop(client_sock, None)
    for client_sock in stale_sockets:
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client_sock.close()
        except OSError:
            pass
    return len(stale_sockets)


def _accept_loop(sock, generation):
    global _server_socket, _server_thread, _running, _bound_port, _last_server_error, _generation
    unexpected_error = None
    while _running and generation == _generation:
        try:
            conn, _addr = sock.accept()
        except OSError as exc:
            if _running and generation == _generation:
                unexpected_error = exc
            break
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            conn.settimeout(CLIENT_SOCKET_TIMEOUT)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            continue
        with _client_sockets_lock:
            if len(_client_sockets) >= MAX_CLIENTS:
                accepted = False
            else:
                _client_sockets[conn] = generation
                accepted = True
        if not accepted:
            try:
                _send_response(
                    conn,
                    {
                        "id": None,
                        "status": "error",
                        "error": f"Too many clients (maximum {MAX_CLIENTS})",
                    },
                )
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            continue
        try:
            threading.Thread(target=_handle_client, args=(conn, generation), daemon=True).start()
        except BaseException:
            with _client_sockets_lock:
                _client_sockets.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass
            continue
    if unexpected_error is not None and generation == _generation:
        _running = False
        _bound_port = None
        _generation += 1
        _last_server_error = f"Listener stopped unexpectedly: {_safe_exception_text(unexpected_error)}"
        if _server_socket is sock:
            _server_socket = None
        try:
            sock.close()
        except OSError:
            pass
        _close_client_sockets(generation)
    if _server_thread is threading.current_thread():
        _server_thread = None


def start_server(port):
    global _server_socket, _server_thread, _running, _timer_registered
    global _generation, _bound_port, _last_server_error
    if _running:
        if _server_thread is not None and _server_thread.is_alive():
            return
        stop_server()
    port = _require_int(port, "port", minimum=1024, maximum=65535)
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(MAX_CLIENTS)
    except OSError as exc:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        raise RuntimeError(
            f"Could not listen on {HOST}:{port} ({_safe_exception_text(exc)})"
        ) from exc
    _server_socket = sock
    _running = True
    _bound_port = port
    _last_server_error = None
    _generation += 1
    generation = _generation
    try:
        _server_thread = threading.Thread(
            target=_accept_loop, args=(sock, generation), daemon=True
        )
        _server_thread.start()
    except BaseException as exc:
        _server_thread = None
        stop_server()
        raise RuntimeError(f"Could not start listener thread: {_safe_exception_text(exc)}") from exc
    if not _timer_registered:
        try:
            bpy.app.timers.register(_process_queue, persistent=True)
            _timer_registered = True
        except BaseException as exc:
            stop_server()
            raise RuntimeError(
                f"Could not register Blender command timer: {_safe_exception_text(exc)}"
            ) from exc


def stop_server():
    global _server_socket, _server_thread, _running, _timer_registered
    global _generation, _bound_port, _last_server_error
    _running = False
    _bound_port = None
    _last_server_error = None
    _generation += 1  # invalidate any in-flight threads from the old generation
    if _server_socket is not None:
        # shutdown() before close(): merely closing a listening socket does
        # not wake a thread blocked in accept() on Linux - the old listener
        # would linger, hold the port, and can make an immediate restart
        # fail to bind.
        try:
            _server_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            _server_socket.close()
        except OSError:
            pass
        _server_socket = None
    server_thread = _server_thread
    _server_thread = None
    if server_thread is not None and server_thread is not threading.current_thread():
        server_thread.join(timeout=1.0)
    _close_client_sockets()
    # Drain any commands still queued so a fast restart can't execute them
    # against a server generation that never actually enqueued them.
    while True:
        try:
            command, response_box, _command_generation = _command_queue.get_nowait()
        except queue.Empty:
            break
        request_id = command.get("id") if isinstance(command, dict) else None
        response_box.put({
            "id": request_id,
            "status": "error",
            "error": "Server was stopped before this command executed",
        })
    if _timer_registered:
        if bpy.app.timers.is_registered(_process_queue):
            bpy.app.timers.unregister(_process_queue)
        _timer_registered = False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MCP_PG_settings(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="Port", default=9876, min=1024, max=65535)
    auto_start: bpy.props.BoolProperty(
        name="Auto-start on load",
        description="Start the MCP server automatically when this file is opened "
        "or the add-on is enabled. Failures are non-blocking and shown here.",
        default=False,
    )
    poll_interval_active: bpy.props.FloatProperty(
        name="Active poll interval",
        description="Seconds between command-queue checks while commands are pending",
        default=0.0, min=0.0, max=1.0,
    )
    poll_interval_idle: bpy.props.FloatProperty(
        name="Idle poll interval",
        description="Seconds between command-queue checks while idle, to avoid "
        "excessive overhead",
        default=0.05, min=0.0, max=5.0,
    )


class MCP_OT_start(bpy.types.Operator):
    bl_idname = "mcp.start_server"
    bl_label = "Start MCP Server"

    def execute(self, context):
        settings = context.scene.blender_mcp_settings
        try:
            start_server(settings.port)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"MCP server listening on {HOST}:{_bound_port}")
        return {"FINISHED"}


class MCP_OT_stop(bpy.types.Operator):
    bl_idname = "mcp.stop_server"
    bl_label = "Stop MCP Server"

    def execute(self, context):
        stop_server()
        self.report({"INFO"}, "MCP server stopped")
        return {"FINISHED"}


class MCP_PT_panel(bpy.types.Panel):
    bl_label = "Blender MCP Bridge"
    bl_idname = "MCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP"

    def draw(self, context):
        # Drawn from the module state rather than a scene property: the
        # server outlives scene/file switches, so a stored flag would lie.
        layout = self.layout
        settings = context.scene.blender_mcp_settings
        if _running:
            layout.label(text=f"Listening on {HOST}:{_bound_port}", icon="CHECKMARK")
            layout.operator("mcp.stop_server")
        else:
            if _last_server_error:
                layout.label(text=_last_server_error, icon="ERROR")
            layout.prop(settings, "port")
            layout.operator("mcp.start_server")
        layout.prop(settings, "auto_start")
        col = layout.column(align=True)
        col.prop(settings, "poll_interval_active")
        col.prop(settings, "poll_interval_idle")


_classes = (MCP_PG_settings, MCP_OT_start, MCP_OT_stop, MCP_PT_panel)


def _auto_start_handler(*_args, **_kwargs):
    """Non-blocking: failures are recorded for the panel, never raised, so a
    bad port/auto-start setting can't stop a file from loading."""
    global _last_server_error
    try:
        scene = bpy.context.scene
        settings = getattr(scene, "blender_mcp_settings", None) if scene else None
        if settings is None or not settings.auto_start or _running:
            return
        start_server(settings.port)
    except BaseException as exc:
        _last_server_error = f"Auto-start failed: {_safe_exception_text(exc)}"


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.blender_mcp_settings = bpy.props.PointerProperty(type=MCP_PG_settings)
    if _auto_start_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_auto_start_handler)
    _auto_start_handler()


def unregister():
    if _auto_start_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_auto_start_handler)
    stop_server()
    del bpy.types.Scene.blender_mcp_settings
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
