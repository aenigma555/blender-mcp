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
  call over TCP to the add-on.

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
claude mcp add blender -- uv --directory /home/aenigma/claude-plugin/server run blender_mcp_server.py
```

(or add it to Claude Desktop's `claude_desktop_config.json` the same way).

Then, with Blender open and the add-on's server started, ask Claude to
build something — e.g. "add a cube, give it a red glossy material, add a
sun light, and render it."

## Available tools

| Tool | Purpose |
|---|---|
| `get_scene_info` | List all objects and their transforms |
| `get_object_info` | Mesh stats, modifiers, materials for one object |
| `add_primitive` | Add cube/sphere/cylinder/cone/plane/torus/monkey |
| `delete_object` | Remove an object |
| `set_transform` | Move/rotate/scale an object |
| `create_material` | Create/update a Principled BSDF material |
| `assign_material` | Assign a material to an object |
| `add_light` | Add point/sun/spot/area light |
| `set_camera` | Create/position a camera |
| `render_scene` | Render to PNG, returned as an image (has its own `timeout` for long renders) |
| `get_viewport_screenshot` | Quick viewport capture, matches current on-screen shading |
| `add_capsule` | Add a cylinder (+ optional rounded caps) aligned between two world-space points — use for limbs/bones instead of hand-computing rotations |
| `mirror_object` | Duplicate an object, reflecting its world transform across an axis-aligned plane through the origin |
| `parent_object` | Parent one object to another, optionally preserving world transform |
| `join_objects` | Join multiple mesh objects into one |
| `undo` | Undo the last N Blender undo steps |
| `execute_code` | Escape hatch: run arbitrary `bpy`/`bmesh` code |
| `save_file` | Save the `.blend` file |

`execute_code` is the escape hatch for anything not covered above —
bmesh editing, modifiers, geometry nodes, UV unwrapping, etc. Set a
variable named `result` in the code to return data from it.

Mesh-creation and mesh-combining commands (`add_primitive`, `add_capsule`,
`join_objects`) require Blender to be in Object Mode and will reject the
call otherwise, rather than risk mutating whatever mesh is currently being
edited.

`undo` maps to Blender's native undo stack. Most commands push exactly one
step, but a compound command (e.g. `add_capsule` with `caps=True`, or
`join_objects`) may push several internal steps and need more than one
`undo` call to fully reverse.

## Notes

- Only one Blender instance should have the server started at a time
  per port (change the port in the sidebar panel + `BLENDER_MCP_PORT`
  env var if you need more).
- The bridge trusts whatever code is sent to `execute_code` — don't
  expose the TCP port beyond localhost.
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
