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

import bpy

HOST = "127.0.0.1"

_server_socket = None
_server_thread = None
_running = False
_command_queue = queue.Queue()
_timer_registered = False


# ---------------------------------------------------------------------------
# Command handlers (always executed on Blender's main thread via the timer)
# ---------------------------------------------------------------------------

def _obj_summary(obj):
    return {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
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
    obj = bpy.data.objects.get(name)
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
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"No object named '{name}'")
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def cmd_set_transform(params):
    name = params["name"]
    obj = bpy.data.objects.get(name)
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
    if bsdf is not None:
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
    obj = bpy.data.objects.get(params["object_name"])
    if obj is None:
        raise ValueError(f"No object named '{params['object_name']}'")
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
    cam = bpy.data.objects.get(name)
    if cam is None or cam.type != "CAMERA":
        cam_data = bpy.data.cameras.new(name)
        cam = bpy.data.objects.new(name, cam_data)
        bpy.context.collection.objects.link(cam)
    if "location" in params:
        cam.location = params["location"]
    if "rotation" in params:
        cam.rotation_euler = params["rotation"]
    if "lens" in params:
        cam.data.lens = params["lens"]
    if params.get("make_active", True):
        bpy.context.scene.camera = cam
    return _obj_summary(cam)


def cmd_render_scene(params):
    scene = bpy.context.scene
    scene.render.resolution_x = params.get("resolution_x", 1024)
    scene.render.resolution_y = params.get("resolution_y", 1024)
    if "samples" in scene.cycles.__dir__() or hasattr(scene, "cycles"):
        try:
            scene.cycles.samples = params.get("samples", 64)
        except Exception:
            pass
    filepath = params.get("filepath", "/tmp/blender_mcp_render.png")
    scene.render.filepath = filepath
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)
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
                    bpy.ops.screen.screenshot_area(filepath=filepath)
                with open(filepath, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
                return {"filepath": filepath, "image_base64": data}
    raise RuntimeError("No VIEW_3D area found to screenshot")


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
            response_box.put({"status": "ok", "result": data})
        except Exception as exc:
            response_box.put({
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
    return 0.05  # reschedule


def _handle_client(conn):
    buf = b""
    try:
        while _running:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    command = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    conn.sendall((json.dumps({"status": "error", "error": f"bad json: {exc}"}) + "\n").encode("utf-8"))
                    continue
                response_box = queue.Queue()
                _command_queue.put((command, response_box))
                response = response_box.get()
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
    except (ConnectionResetError, OSError):
        pass
    finally:
        conn.close()


def _accept_loop():
    global _server_socket
    while _running:
        try:
            conn, _addr = _server_socket.accept()
        except OSError:
            break
        threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()


def start_server(port):
    global _server_socket, _server_thread, _running, _timer_registered
    if _running:
        return
    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _server_socket.bind((HOST, port))
    _server_socket.listen(5)
    _running = True
    _server_thread = threading.Thread(target=_accept_loop, daemon=True)
    _server_thread.start()
    if not _timer_registered:
        bpy.app.timers.register(_process_queue, persistent=True)
        _timer_registered = True


def stop_server():
    global _server_socket, _running, _timer_registered
    _running = False
    if _server_socket is not None:
        try:
            _server_socket.close()
        except OSError:
            pass
        _server_socket = None
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
        start_server(settings.port)
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
