bl_info = {
    "name": "Blender MCP Bridge",
    "author": "claude",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > MCP",
    "description": "Runs a local TCP command server so an external MCP server can drive Blender",
    "category": "Interface",
}

import base64
import json
import queue
import socket
import threading
import traceback

import bmesh
import bpy
from mathutils import Matrix, Vector

HOST = "127.0.0.1"
MAX_LINE_BYTES = 64 * 1024 * 1024  # safety cap on a single buffered command line

_server_socket = None
_server_thread = None
_running = False
_command_queue = queue.Queue()
_timer_registered = False
_client_sockets_lock = threading.Lock()
_client_sockets = set()
_generation = 0


# ---------------------------------------------------------------------------
# Command handlers (always executed on Blender's main thread via the timer)
# ---------------------------------------------------------------------------

def _get_scene_object(name):
    return bpy.context.scene.objects.get(name)


def _require_object_mode():
    if bpy.context.mode != 'OBJECT':
        raise RuntimeError(
            f"Blender is in '{bpy.context.mode}' mode; switch to Object Mode before running this command"
        )


def _obj_summary(obj):
    if obj.rotation_mode == 'QUATERNION':
        rotation_euler = list(obj.rotation_quaternion.to_euler())
    else:
        rotation_euler = list(obj.rotation_euler)
    return {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": rotation_euler,
        "rotation_quaternion": list(obj.rotation_quaternion) if obj.rotation_mode == 'QUATERNION' else None,
        "scale": list(obj.scale),
        "dimensions": list(obj.dimensions),
        "visible": obj.visible_get(),
        "parent": obj.parent.name if obj.parent else None,
    }


def cmd_get_scene_info(params):
    scene = bpy.context.scene
    return {
        "scene_name": scene.name,
        "frame_current": scene.frame_current,
        "objects": [_obj_summary(o) for o in scene.objects],
        "active_object": scene.view_layers[0].objects.active.name
        if scene.view_layers[0].objects.active else None,
    }


def cmd_get_object_info(params):
    name = params["name"]
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
    info["materials"] = [m.name for m in obj.data.materials] if hasattr(obj.data, "materials") else []
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
    prim_type = params["type"]
    if prim_type not in _PRIMITIVES:
        raise ValueError(f"Unknown primitive type '{prim_type}'. Options: {list(_PRIMITIVES)}")
    location = tuple(params.get("location", (0, 0, 0)))
    rotation = tuple(params.get("rotation", (0, 0, 0)))
    scale = tuple(params.get("scale", (1, 1, 1)))
    _PRIMITIVES[prim_type](location=location, rotation=rotation)
    obj = bpy.context.active_object
    obj.scale = scale
    if "name" in params:
        obj.name = params["name"]
    return _obj_summary(obj)


def cmd_delete_object(params):
    name = params["name"]
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def cmd_set_transform(params):
    name = params["name"]
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    if "location" in params:
        obj.location = params["location"]
    if "rotation" in params:
        obj.rotation_euler = params["rotation"]
    if "scale" in params:
        obj.scale = params["scale"]
    return _obj_summary(obj)


def cmd_create_material(params):
    name = params["name"]
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    requested_props = {"base_color", "metallic", "roughness", "emission_color", "emission_strength"}
    if bsdf is None:
        if requested_props & params.keys():
            raise ValueError(
                f"Material '{name}' has no 'Principled BSDF' node (custom node setup?); "
                "cannot set the requested properties"
            )
        return {"material": mat.name}
    if "base_color" in params:
        r, g, b, *a = params["base_color"] + [1.0]
        bsdf.inputs["Base Color"].default_value = (r, g, b, a[0] if a else 1.0)
    if "metallic" in params:
        bsdf.inputs["Metallic"].default_value = params["metallic"]
    if "roughness" in params:
        bsdf.inputs["Roughness"].default_value = params["roughness"]
    if "emission_color" in params:
        bsdf.inputs["Emission Color"].default_value = (*params["emission_color"], 1.0)
    if "emission_strength" in params:
        bsdf.inputs["Emission Strength"].default_value = params["emission_strength"]
    return {"material": mat.name}


def cmd_assign_material(params):
    obj = _get_scene_object(params["object_name"])
    if obj is None:
        raise ValueError(f"No object named '{params['object_name']}'")
    if obj.data is None or not hasattr(obj.data, "materials"):
        raise ValueError(f"Object '{obj.name}' (type {obj.type}) does not support materials")
    mat = bpy.data.materials.get(params["material_name"])
    if mat is None:
        raise ValueError(f"No material named '{params['material_name']}'")
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return {"object": obj.name, "material": mat.name}


def cmd_add_light(params):
    light_type = params.get("type", "POINT").upper()
    name = params.get("name", "Light")
    light_data = bpy.data.lights.new(name=name, type=light_type)
    light_data.energy = params.get("energy", 1000.0)
    if "color" in params:
        light_data.color = params["color"]
    light_obj = bpy.data.objects.new(name=name, object_data=light_data)
    bpy.context.collection.objects.link(light_obj)
    light_obj.location = params.get("location", (0, 0, 5))
    return _obj_summary(light_obj)


def cmd_set_camera(params):
    name = params.get("name", "Camera")
    cam = _get_scene_object(name)
    if cam is not None and cam.type != "CAMERA":
        raise ValueError(f"Object '{name}' exists but is type {cam.type}, not CAMERA")
    if cam is None:
        cam_data = bpy.data.cameras.new(name)
        cam = bpy.data.objects.new(name, cam_data)
        bpy.context.collection.objects.link(cam)
    if "location" in params:
        cam.location = params["location"]
    if "rotation" in params:
        cam.rotation_euler = params["rotation"]
    if "lens" in params:
        if params["lens"] <= 0:
            raise ValueError("lens must be positive")
        cam.data.lens = params["lens"]
    if params.get("make_active", True):
        bpy.context.scene.camera = cam
    return _obj_summary(cam)


def cmd_render_scene(params):
    scene = bpy.context.scene
    resolution_x = params.get("resolution_x", 1024)
    resolution_y = params.get("resolution_y", 1024)
    samples = params.get("samples", 64)
    if resolution_x <= 0 or resolution_y <= 0:
        raise ValueError("resolution_x and resolution_y must be positive")
    if samples <= 0:
        raise ValueError("samples must be positive")
    scene.render.resolution_x = resolution_x
    scene.render.resolution_y = resolution_y
    scene.render.resolution_percentage = 100

    engine = scene.render.engine
    if engine == "CYCLES" and hasattr(scene, "cycles"):
        scene.cycles.samples = samples
    elif engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT") and hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = samples

    filepath = bpy.path.abspath(params.get("filepath", "/tmp/blender_mcp_render.png"))
    scene.render.filepath = filepath
    scene.render.image_settings.file_format = "PNG"
    scene.render.use_file_extension = False
    render_result = bpy.ops.render.render(write_still=True)
    if 'FINISHED' not in render_result:
        raise RuntimeError(f"Render did not complete (result: {render_result!r})")
    result = {"filepath": filepath}
    if params.get("return_image", True):
        with open(filepath, "rb") as f:
            result["image_base64"] = base64.b64encode(f.read()).decode("ascii")
    return result


def cmd_get_viewport_screenshot(params):
    filepath = params.get("filepath", "/tmp/blender_mcp_viewport.png")
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
    return {
        "active": bpy.context.view_layer.objects.active,
        "selected": list(bpy.context.selected_objects),
    }


def _restore_selection(state):
    bpy.ops.object.select_all(action='DESELECT')
    for obj in state["selected"]:
        if obj and obj.name in bpy.data.objects:
            obj.select_set(True)
    active = state["active"]
    if active and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active


def cmd_add_capsule(params):
    _require_object_mode()
    start = Vector(params["start"])
    end = Vector(params["end"])
    radius = params.get("radius", 0.1)
    if radius <= 0:
        raise ValueError("radius must be positive")
    name = params.get("name") or "Capsule"
    caps = params.get("caps", True)

    direction = end - start
    length = direction.length
    if length < 1e-6:
        raise ValueError("start and end must differ")
    d = direction.normalized()
    center = (start + end) / 2

    prev = _save_selection()
    cyl_result = bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=length, location=center)
    if 'FINISHED' not in cyl_result:
        raise RuntimeError(f"Failed to create cylinder (result: {cyl_result!r})")
    cyl = bpy.context.active_object
    cyl.name = name
    z = Vector((0, 0, 1))
    cyl.rotation_mode = 'QUATERNION'
    cyl.rotation_quaternion = z.rotation_difference(d)

    if caps:
        parts = [cyl]
        try:
            bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=start)
            cap_start = bpy.context.active_object
            cap_start.name = f"{name}_cap_start"
            parts.append(cap_start)

            bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=end)
            cap_end = bpy.context.active_object
            cap_end.name = f"{name}_cap_end"
            parts.append(cap_end)

            bpy.ops.object.select_all(action='DESELECT')
            for p in parts:
                p.select_set(True)
            bpy.context.view_layer.objects.active = cyl
            bpy.ops.object.join()
            cyl.name = name
        except Exception:
            for p in parts:
                if p.name in bpy.data.objects:
                    bpy.data.objects.remove(p, do_unlink=True)
            _restore_selection(prev)
            raise

    _restore_selection(prev)
    return _obj_summary(cyl)


def cmd_mirror_object(params):
    name = params["name"]
    obj = _get_scene_object(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    if obj.type != "MESH":
        raise ValueError(f"Object '{name}' is type {obj.type}; mirror_object only supports MESH objects")
    axis = params.get("axis", "X").upper()
    axis_index = {"X": 0, "Y": 1, "Z": 2}.get(axis)
    if axis_index is None:
        raise ValueError(f"axis must be one of X, Y, Z, got '{axis}'")

    new_obj = obj.copy()
    new_obj.data = obj.data.copy()
    new_obj.name = params.get("new_name") or f"{name}_mirror"
    bpy.context.collection.objects.link(new_obj)

    try:
        # Reflect the object's full world-space transform (position and
        # rotation) across the world-origin plane perpendicular to axis, so
        # this is correct even for rotated or parented objects.
        reflect = Matrix.Identity(4)
        reflect[axis_index][axis_index] = -1.0
        new_obj.matrix_world = reflect @ obj.matrix_world

        # The reflection has negative determinant, which flips face winding
        # in world space; flip it back in local mesh data so it renders
        # right-side-out.
        bm = bmesh.new()
        try:
            bm.from_mesh(new_obj.data)
            for f in bm.faces:
                f.normal_flip()
            bm.normal_update()
            bm.to_mesh(new_obj.data)
        finally:
            bm.free()
        new_obj.data.update()
    except Exception:
        mesh_data = new_obj.data
        bpy.data.objects.remove(new_obj, do_unlink=True)
        if mesh_data.users == 0:
            bpy.data.meshes.remove(mesh_data)
        raise

    return _obj_summary(new_obj)


def cmd_parent_object(params):
    child = _get_scene_object(params["child"])
    if child is None:
        raise ValueError(f"No object named '{params['child']}'")
    parent = _get_scene_object(params["parent"])
    if parent is None:
        raise ValueError(f"No object named '{params['parent']}'")
    if params.get("keep_transform", True):
        # Capture the true world matrix first (matrix_basis alone is only
        # meaningful relative to whatever parent the child had before, if
        # any), then re-derive matrix_basis against the new parent with a
        # clean identity parent-inverse.
        world_matrix = child.matrix_world.copy()
        child.parent = parent
        child.matrix_parent_inverse = Matrix.Identity(4)
        child.matrix_world = world_matrix
    else:
        child.parent = parent
        child.matrix_parent_inverse = Matrix.Identity(4)
    return {"child": child.name, "parent": parent.name}


def cmd_join_objects(params):
    _require_object_mode()
    names = params["names"]
    if len(set(names)) != len(names):
        raise ValueError("names must not contain duplicates")
    objs = []
    for n in names:
        obj = _get_scene_object(n)
        if obj is None:
            raise ValueError(f"No object named '{n}'")
        if obj.type != "MESH":
            raise ValueError(f"Object '{n}' is type {obj.type}; join_objects only supports MESH objects")
        objs.append(obj)
    if len(objs) < 2:
        raise ValueError("Need at least 2 objects to join")
    prev = _save_selection()
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    join_result = bpy.ops.object.join()
    if 'FINISHED' not in join_result:
        _restore_selection(prev)
        raise RuntimeError(f"Join did not complete (result: {join_result!r})")
    joined = bpy.context.active_object
    target_name = params.get("target_name")
    if target_name:
        joined.name = target_name
    return _obj_summary(joined)


def cmd_undo(params):
    steps = params.get("steps", 1)
    if not isinstance(steps, int) or steps < 1:
        raise ValueError("steps must be a positive integer")
    performed = 0
    for _ in range(steps):
        try:
            result = bpy.ops.ed.undo()
        except RuntimeError:
            break
        if 'FINISHED' not in result:
            break
        performed += 1
    return {"steps_requested": steps, "steps_performed": performed}


def cmd_execute_code(params):
    code = params["code"]
    local_ns = {"bpy": bpy}
    exec(compile(code, "<mcp_execute_code>", "exec"), local_ns, local_ns)
    result = local_ns.get("result")
    return {"result": result}


def cmd_save_file(params):
    filepath = params["filepath"]
    bpy.ops.wm.save_as_mainfile(filepath=filepath)
    return {"saved": filepath}


_MUTATING_COMMANDS = {
    "add_primitive", "delete_object", "set_transform", "create_material",
    "assign_material", "add_light", "set_camera", "add_capsule",
    "mirror_object", "parent_object", "join_objects", "execute_code",
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
    "undo": cmd_undo,
    "execute_code": cmd_execute_code,
    "save_file": cmd_save_file,
}


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

def _process_queue():
    """Runs on Blender's main thread via bpy.app.timers."""
    processed = 0
    while processed < 20:
        try:
            command, response_box = _command_queue.get_nowait()
        except queue.Empty:
            break
        processed += 1
        try:
            handler = _HANDLERS.get(command.get("type"))
            if handler is None:
                raise ValueError(f"Unknown command type '{command.get('type')}'")
            data = handler(command.get("params", {}))
            if command.get("type") in _MUTATING_COMMANDS:
                # Many of our commands mutate bpy.data directly rather than
                # through operators, which otherwise leaves no undo step at
                # all for `undo` to land on. Push one explicit boundary per
                # command so it's always addressable, even though commands
                # that internally run several operators (e.g. add_capsule
                # with caps, join_objects) may still need more than one
                # `undo` call to fully unwind.
                try:
                    bpy.ops.ed.undo_push(message=f"MCP: {command.get('type')}")
                except RuntimeError:
                    pass
            response_box.put({"status": "ok", "result": data})
        except Exception as exc:
            response_box.put({
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
    return 0.05  # reschedule


def _send_response(conn, response):
    try:
        payload = json.dumps(response)
    except (TypeError, ValueError) as exc:
        payload = json.dumps({"status": "error", "error": f"Result not JSON-serializable: {exc}"})
    conn.sendall((payload + "\n").encode("utf-8"))


def _handle_client(conn, generation):
    with _client_sockets_lock:
        _client_sockets.add(conn)
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
            if len(buf) > MAX_LINE_BYTES:
                try:
                    _send_response(conn, {"status": "error", "error": "Command exceeds max size"})
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
                        _send_response(conn, {"status": "error", "error": f"bad json: {exc}"})
                    except OSError:
                        pass
                    continue
                response_box = queue.Queue()
                _command_queue.put((command, response_box))
                response = None
                while _running and generation == _generation:
                    try:
                        response = response_box.get(timeout=0.5)
                        break
                    except queue.Empty:
                        continue
                if response is None:
                    response = {"status": "error", "error": "Server is stopping"}
                try:
                    _send_response(conn, response)
                except OSError:
                    break
    except (ConnectionResetError, OSError):
        pass
    finally:
        with _client_sockets_lock:
            _client_sockets.discard(conn)
        conn.close()


def _accept_loop(sock, generation):
    while _running and generation == _generation:
        try:
            conn, _addr = sock.accept()
        except OSError:
            break
        threading.Thread(target=_handle_client, args=(conn, generation), daemon=True).start()


def start_server(port):
    global _server_socket, _server_thread, _running, _timer_registered, _generation
    if _running:
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
    except OSError as exc:
        sock.close()
        raise RuntimeError(f"Could not bind to {HOST}:{port} ({exc})") from exc
    sock.listen(5)
    _server_socket = sock
    _running = True
    _generation += 1
    generation = _generation
    _server_thread = threading.Thread(target=_accept_loop, args=(sock, generation), daemon=True)
    _server_thread.start()
    if not _timer_registered:
        bpy.app.timers.register(_process_queue, persistent=True)
        _timer_registered = True


def stop_server():
    global _server_socket, _running, _timer_registered, _generation
    _running = False
    _generation += 1  # invalidate any in-flight threads from the old generation
    if _server_socket is not None:
        try:
            _server_socket.close()
        except OSError:
            pass
        _server_socket = None
    with _client_sockets_lock:
        stale_sockets = list(_client_sockets)
        _client_sockets.clear()
    for sock in stale_sockets:
        try:
            sock.close()
        except OSError:
            pass
    # Drain any commands still queued so a fast restart can't execute them
    # against a server generation that never actually enqueued them.
    while True:
        try:
            _, response_box = _command_queue.get_nowait()
        except queue.Empty:
            break
        response_box.put({"status": "error", "error": "Server was stopped before this command executed"})
    if _timer_registered:
        if bpy.app.timers.is_registered(_process_queue):
            bpy.app.timers.unregister(_process_queue)
        _timer_registered = False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MCP_PG_settings(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="Port", default=9876, min=1024, max=65535)
    running: bpy.props.BoolProperty(name="Running", default=False)


class MCP_OT_start(bpy.types.Operator):
    bl_idname = "mcp.start_server"
    bl_label = "Start MCP Server"

    def execute(self, context):
        settings = context.scene.blender_mcp_settings
        try:
            start_server(settings.port)
        except RuntimeError as exc:
            settings.running = False
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.running = True
        self.report({"INFO"}, f"MCP server listening on {HOST}:{settings.port}")
        return {"FINISHED"}


class MCP_OT_stop(bpy.types.Operator):
    bl_idname = "mcp.stop_server"
    bl_label = "Stop MCP Server"

    def execute(self, context):
        stop_server()
        context.scene.blender_mcp_settings.running = False
        self.report({"INFO"}, "MCP server stopped")
        return {"FINISHED"}


class MCP_PT_panel(bpy.types.Panel):
    bl_label = "Blender MCP Bridge"
    bl_idname = "MCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.blender_mcp_settings
        layout.prop(settings, "port")
        if settings.running:
            layout.label(text=f"Listening on {HOST}:{settings.port}", icon="CHECKMARK")
            layout.operator("mcp.stop_server")
        else:
            layout.operator("mcp.start_server")


_classes = (MCP_PG_settings, MCP_OT_start, MCP_OT_stop, MCP_PT_panel)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.blender_mcp_settings = bpy.props.PointerProperty(type=MCP_PG_settings)


def unregister():
    stop_server()
    del bpy.types.Scene.blender_mcp_settings
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
