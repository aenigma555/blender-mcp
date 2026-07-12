# Blender MCP Bridge

Lets Claude drive a running Blender instance directly: create/edit meshes,
set up materials, lights and cameras, render, and inspect the scene — all
through MCP tool calls instead of copy-pasted scripts.

## Architecture

```
Claude  <--MCP (stdio)-->  server/blender_mcp_server.py  <--TCP JSON-->  addon/blender_mcp_addon.py (inside Blender)
```

- **addon/blender_mcp_addon.py** — a Blender add-on. It opens a TCP socket
  server inside Blender and executes incoming commands on Blender's main
  thread (via `bpy.app.timers`), so it's safe to call `bpy` from it.
- **server/blender_mcp_server.py** — a standalone MCP server (Python, using
  the official `mcp` SDK). It exposes tools like `add_primitive`,
  `create_material`, `render_scene`, `execute_code`, etc., and forwards each
  call over TCP to the add-on. Calls carry stable request IDs and the add-on
  deduplicates repeated IDs through a bounded response cache. Read-only
  inspection calls reconnect and retry once after non-timeout connection
  failures; sent mutations are never replayed automatically.

## Setup

### 1. Install the Blender add-on

Blender > Edit > Preferences > Add-ons > Install..., pick
`addon/blender_mcp_addon.py`, enable "Blender MCP Bridge".

Open the 3D Viewport sidebar (`N`), go to the **MCP** tab, click
**Start MCP Server**. It listens on `127.0.0.1:9876` by default.

### 2. Install the MCP server's dependencies

```bash
cd server
uv sync            # or: pip install -e .
```

### 3. Register it with Claude

```bash
claude mcp add blender -- uv --directory /absolute/path/to/claude-plugin/server run blender_mcp_server.py
```

Replace `/absolute/path/to/claude-plugin` with this repository's absolute
path. If you installed with `pip` instead of `uv`, register the Python
interpreter from that environment directly:

```bash
claude mcp add blender -- /absolute/path/to/venv/bin/python /absolute/path/to/claude-plugin/server/blender_mcp_server.py
```

On Windows, use the environment's `Scripts\\python.exe`. You can also put the
equivalent command and arguments in Claude Desktop's
`claude_desktop_config.json`.

Then, with Blender open and the add-on's server started, ask Claude to
build something — e.g. "add a cube, give it a red glossy material, add a
sun light, and render it."

## Available tools

| Tool | Purpose |
|---|---|
| `get_scene_info` | List objects and their transforms (capped by `limit`, default 200, with `object_count`/`truncated` for big scenes) |
| `get_object_info` | Mesh stats, modifiers, materials for one object |
| `add_primitive` | Add cube/sphere/ico-sphere/cylinder/cone/plane/torus/monkey |
| `delete_object` | Remove an object |
| `set_transform` | Move/rotate/scale an object (rotation works in any rotation mode) |
| `create_material` | Create/update a Principled BSDF material: base/emission color, metallic, roughness, alpha, ior, transmission, specular, coat, sheen, subsurface |
| `assign_material` | Assign a material to an object |
| `add_light` | Add point/sun/spot/area light; `rotation` aims directional lights, energy defaults to 1000 W (3 for SUN — sun strength is W/m²) |
| `set_camera` | Create/position a camera; `look_at` aims it at a world-space point so you never hand-compute euler angles |
| `render_scene` | Render to PNG, returned as an image (has its own `timeout` for long renders; the scene's render settings are restored afterwards) |
| `get_viewport_screenshot` | Quick viewport capture, matches current on-screen shading |
| `add_capsule` | Add one connected manifold capsule (or a closed cylinder with `caps=False`) aligned between two world-space points |
| `mirror_object` | Duplicate any object, reflecting its world transform across an axis-aligned plane through the origin (like Blender's Object > Mirror) |
| `parent_object` | Parent one object to another, optionally preserving world transform |
| `join_objects` | Join multiple mesh objects into one; face winding of mirrored (negative-determinant) sources is corrected automatically |
| `set_shading` | Smooth- or flat-shade all faces of a mesh |
| `undo` | Undo the last N Blender undo steps |
| `redo` | Redo the last N undone steps |
| `execute_code` | Escape hatch: run arbitrary `bpy`/`bmesh` code (own `timeout` for long scripts) |
| `save_file` | Save the `.blend` file (save-as with `filepath`, in place without) |

`execute_code` is the escape hatch for anything not covered above —
bmesh editing, modifiers, geometry nodes, UV unwrapping, etc. `bpy`,
`bmesh`, `mathutils`, `Vector`, `Matrix`, `Euler` and `Quaternion` are
predefined; set a variable named `result` in the code to return data
(non-JSON-serializable results come back as their `repr`).

Commands that create, join, or destructively edit objects
(`add_primitive`, `add_capsule`, `join_objects`, `delete_object`,
`mirror_object`, `set_shading`) require Blender to be in Object Mode and
will reject the call otherwise, rather than risk mutating whatever mesh is
currently being edited.

`undo` maps to Blender's native undo stack. Most commands push exactly one
step, but an operator-backed compound command such as `join_objects` may push
internal steps and need more than one `undo` call to fully reverse.

### Mirroring and normals

`mirror_object` reflects the object's world transform, producing a
negative-determinant matrix — exactly what Blender's own Object > Mirror
does. Cycles and EEVEE render that correctly as-is (verified empirically
with a Backfacing probe), so the mesh data is left untouched. The winding
only becomes "real" when the transform gets baked into vertices — which is
what `join_objects` does — so `join_objects` flips the winding of any
source whose transform relative to the join target has negative
determinant. Net effect: mirror + join produces outward normals with no
manual fixing.

## Testing

Run the protocol tests without Blender:

```bash
cd server
uv run python -m unittest discover -s tests -v
```

With the pip setup, activate that environment and run
`python -m unittest discover -s tests -v` instead.

Run the add-on regressions against the installed Blender version:

```bash
blender --background --factory-startup --python-exit-code 1 \
  --python /absolute/path/to/claude-plugin/addon/tests/headless_regression.py
```

The Blender suite covers capsule topology, mirror/join normals, validation,
request deduplication, stale server generations, and timer-safe exception
handling.

## Notes

- Each command has a unique request ID, and Blender returns a cached response
  when it receives the same ID and payload again. Cached responses are bounded
  and expire, so request IDs are not a permanent transaction log. The MCP
  server automatically retries only `get_scene_info` and `get_object_info`
  after sending; a sent mutation that loses its connection reports an unknown
  outcome instead of risking a replay after cache eviction.
- A timeout means the outcome is unknown, not that Blender cancelled the work.
  The MCP server closes the connection and does not retry a timed-out command,
  but Blender may still finish it. Inspect the scene before issuing another
  mutation.
- Only one Blender instance should have the server started at a time
  per port (change the port in the sidebar panel + `BLENDER_MCP_PORT`
  env var if you need more; `BLENDER_MCP_HOST` exists too but the add-on
  only listens on localhost).
- Default render/screenshot paths live in the platform temp directory
  (`tempfile.gettempdir()`), not a hardcoded `/tmp`.
- Localhost is the bridge's trust boundary, not an authentication mechanism.
  Any local process that can reach the port can invoke tools, and
  `execute_code` can run arbitrary Python with Blender/user permissions. Do
  not proxy, tunnel, or otherwise expose the port to untrusted clients.
- To protect Blender's interactive session, the add-on bounds command and
  response sizes, queued work, concurrent client handlers, render dimensions,
  and its request-response cache. Requests are rejected when those limits are
  exceeded; split large work into smaller commands instead.
- **Don't hot-reload the addon module from inside a running session**
  (e.g. via `execute_code` re-exec'ing its own source). It's tempting
  after editing `addon/blender_mcp_addon.py`, but patching the module
  while the very connection making the request is served by it can
  corrupt the server's socket/thread state and require a full Blender
  restart to recover. Instead, reload cleanly: disable and re-enable
  "Blender MCP Bridge" under Preferences > Add-ons, or restart Blender,
  then click **Start MCP Server** again. After editing
  `server/blender_mcp_server.py`, restart the Claude Code session (or
  reconnect the MCP server) to pick up new tool schemas.
