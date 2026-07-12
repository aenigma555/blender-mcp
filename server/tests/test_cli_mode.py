"""Tests for the CLI/background-mode subprocess orchestration.

These mock subprocess.run entirely, so they run without a real Blender
install and without spawning any process. The mocked side effect simulates
what blender_mcp_cli_runner.py actually does: write a JSON response to the
output_path passed as the final argv element.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest
from unittest import mock


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import blender_mcp_server as server  # noqa: E402


def _write_runner_output(argv, response):
    output_path = argv[-1]
    Path(output_path).write_text(json.dumps(response), encoding="utf-8")


class CliModeTests(unittest.TestCase):
    def setUp(self):
        self._blend_file = tempfile.NamedTemporaryFile(suffix=".blend", delete=False)
        self._blend_file.close()
        self.blend_file = self._blend_file.name
        self._original_executable = server.BLENDER_EXECUTABLE
        server.BLENDER_EXECUTABLE = "/usr/bin/blender"

    def tearDown(self):
        server.BLENDER_EXECUTABLE = self._original_executable
        Path(self.blend_file).unlink(missing_ok=True)

    def test_success_reads_result_and_cleans_up_output_file(self):
        captured_output_path = []

        def fake_run(argv, **kwargs):
            captured_output_path.append(argv[-1])
            self.assertTrue(Path(argv[-1]).is_file(), "output path was not reserved")
            self.assertEqual(Path(argv[-1]).read_text(encoding="utf-8"), "")
            _write_runner_output(argv, {"status": "ok", "result": {"objects": 3}})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            result = server._run_cli_command(
                "get_blendfile_summary_datablocks", {}, self.blend_file, 30.0
            )

        self.assertEqual(result, {"objects": 3})
        self.assertFalse(Path(captured_output_path[0]).exists(), "output file was not cleaned up")

    def test_oversized_output_is_rejected_before_json_parsing(self):
        original_max_response_bytes = server.MAX_RESPONSE_BYTES
        server.MAX_RESPONSE_BYTES = 64
        try:
            def fake_run(argv, **kwargs):
                Path(argv[-1]).write_text("x" * 65, encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "oversized result"):
                    server._run_cli_command(
                        "get_blendfile_summary_path_info", {}, self.blend_file, 30.0
                    )
        finally:
            server.MAX_RESPONSE_BYTES = original_max_response_bytes

    def test_argv_encodes_command_and_base64_params(self):
        def fake_run(argv, **kwargs):
            self.assertEqual(argv[0], server.BLENDER_EXECUTABLE)
            self.assertIn("--background", argv)
            self.assertIn("--factory-startup", argv)
            self.assertIn(self.blend_file, argv)
            self.assertIn(str(server.CLI_RUNNER_PATH), argv)
            separator_index = argv.index("--")
            command_type, params_b64, _output_path = argv[separator_index + 1:]
            self.assertEqual(command_type, "execute_code")
            decoded = json.loads(base64.b64decode(params_b64).decode("utf-8"))
            self.assertEqual(decoded, {"code": "result = 1"})
            _write_runner_output(argv, {"status": "ok", "result": {"result": 1}})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            result = server._run_cli_command(
                "execute_code", {"code": "result = 1"}, self.blend_file, 30.0
            )
        self.assertEqual(result, {"result": 1})

    def test_error_response_raises_runtime_error_with_message(self):
        def fake_run(argv, **kwargs):
            _write_runner_output(argv, {"status": "error", "error": "boom"})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, self.blend_file, 30.0
                )

    def test_non_zero_exit_raises_with_stderr(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="segfault happened")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "segfault happened"):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, self.blend_file, 30.0
                )

    def test_missing_output_file_raises(self):
        def fake_run(argv, **kwargs):
            # Simulate a crash before the runner script could write anything.
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="python traceback")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "python traceback"):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, self.blend_file, 30.0
                )

    def test_timeout_raises_timeout_error_and_does_not_leave_output_file(self):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(TimeoutError):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, self.blend_file, 0.01
                )

    def test_missing_blend_file_raises_before_spawning_subprocess(self):
        with mock.patch.object(server.subprocess, "run") as run_mock:
            with self.assertRaises(FileNotFoundError):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, "/no/such/file.blend", 30.0
                )
        run_mock.assert_not_called()

    def test_missing_blender_executable_raises_before_spawning_subprocess(self):
        server.BLENDER_EXECUTABLE = None
        with mock.patch.object(server.subprocess, "run") as run_mock:
            with self.assertRaisesRegex(RuntimeError, "BLENDER_MCP_EXECUTABLE"):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, self.blend_file, 30.0
                )
        run_mock.assert_not_called()

    def test_output_file_cleaned_up_even_on_error_response(self):
        captured_output_path = []

        def fake_run(argv, **kwargs):
            captured_output_path.append(argv[-1])
            _write_runner_output(argv, {"status": "error", "error": "boom"})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                server._run_cli_command(
                    "get_blendfile_summary_path_info", {}, self.blend_file, 30.0
                )
        self.assertFalse(Path(captured_output_path[0]).exists())


if __name__ == "__main__":
    unittest.main()
