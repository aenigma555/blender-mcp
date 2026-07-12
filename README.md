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
| `render_scene` | Render to PNG, returned as an image |
| `get_viewport_screenshot` | Quick unlit viewport capture |
| `execute_code` | Escape hatch: run arbitrary `bpy`/`bmesh` code |
| `save_file` | Save the `.blend` file |

`execute_code` is the escape hatch for anything not covered above —
bmesh editing, modifiers, geometry nodes, UV unwrapping, etc. Set a
variable named `result` in the code to return data from it.

## Notes

- Only one Blender instance should have the server started at a time
  per port (change the port in the sidebar panel + `BLENDER_MCP_PORT`
  env var if you need more).
- The bridge trusts whatever code is sent to `execute_code` — don't
  expose the TCP port beyond localhost.
