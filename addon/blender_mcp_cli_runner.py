"""Standalone entry point for Blender MCP's headless/background command mode.

Invoked by the MCP server (never by hand) as:

    blender --background --factory-startup <blend_file> --python \
        addon/blender_mcp_cli_runner.py -- <command_type> <base64_params> <output_path>

Loads blender_mcp_addon.py directly and runs exactly one command against
whatever file Blender opened on its command line, then writes a JSON
response to <output_path>. The add-on's register()/TCP server/timer are
never touched - this has nothing to do with, and never conflicts with, an
interactive Blender session that happens to have its MCP server running.

Only commands in ADDON.CLI_SAFE_COMMANDS are permitted, since there is no
window/viewport in --background mode and, critically, nobody watching.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

ADDON_PATH = Path(__file__).resolve().parent / "blender_mcp_addon.py"


def _load_addon():
    spec = importlib.util.spec_from_file_location("blender_mcp_addon_cli", ADDON_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load add-on module from {ADDON_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli_args(argv: list[str]) -> list[str]:
    if "--" not in argv:
        raise SystemExit("Missing '--' argument separator before command_type/params/output_path")
    return argv[argv.index("--") + 1:]


def run(addon, command_type: str, params_b64: str, output_path: str) -> None:
    try:
        if command_type not in addon.CLI_SAFE_COMMANDS:
            raise ValueError(f"'{command_type}' is not permitted in CLI/background mode")
        handler = addon._HANDLERS.get(command_type)
        if handler is None:
            raise ValueError(f"Unknown command type '{command_type}'")
        params = json.loads(base64.b64decode(params_b64).decode("utf-8"))
        result = handler(params)
        response = {"status": "ok", "result": result}
    except BaseException as exc:  # noqa: BLE001 - must never crash without writing a response
        response = {
            "status": "error",
            "error": addon._safe_exception_text(exc),
            "traceback": addon._safe_traceback(),
        }

    try:
        payload = json.dumps(response)
    except (TypeError, ValueError) as exc:
        payload = json.dumps({
            "status": "error",
            "error": f"Result not JSON-serializable: {exc}",
        })
    Path(output_path).write_text(payload, encoding="utf-8")


def main() -> None:
    command_type, params_b64, output_path = _cli_args(sys.argv)[:3]
    addon = _load_addon()
    run(addon, command_type, params_b64, output_path)


if __name__ == "__main__":
    main()
