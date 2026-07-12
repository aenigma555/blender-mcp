"""Protocol-level regression tests for :class:`BlenderConnection`.

These tests use small scripted sockets so they can run without Blender.  The
fake peer speaks the same newline-delimited JSON protocol as the add-on.
"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import blender_mcp_server as server  # noqa: E402


class ScriptedSocket:
    """A socket double whose receive script is built from the sent request."""

    def __init__(self, response_builder=None, *, send_error=None):
        self.response_builder = response_builder
        self.send_error = send_error
        self.sent = []
        self.recv_items = deque()
        self.timeouts = []
        self.connected_to = None
        self.closed = False

    def setsockopt(self, *_args):
        pass

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, address):
        self.connected_to = address

    def sendall(self, payload):
        self.sent.append(payload)
        if self.send_error is not None:
            raise self.send_error
        if self.response_builder is not None:
            request = json.loads(payload.decode("utf-8"))
            self.recv_items.extend(self.response_builder(request))

    def recv(self, _size):
        if not self.recv_items:
            return b""
        item = self.recv_items.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def response_bytes(request, *, result=None, response_id=None):
    response = {
        "id": request["id"] if response_id is None else response_id,
        "status": "ok",
        "result": {} if result is None else result,
    }
    return (json.dumps(response) + "\n").encode("utf-8")


class BlenderConnectionProtocolTests(unittest.TestCase):
    def connect_with(self, fake_socket):
        connection = server.BlenderConnection("127.0.0.1", 19876)
        socket_patch = mock.patch.object(server.socket, "socket", return_value=fake_socket)
        return connection, socket_patch

    def test_newline_framing_and_partial_response(self):
        def fragmented_response(request):
            response = response_bytes(request, result={"object_count": 3})
            return [response[:2], response[2:17], response[17:-1], response[-1:]]

        fake_socket = ScriptedSocket(fragmented_response)
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch:
            result = connection.send_command("get_scene_info", {"limit": 25})

        self.assertEqual(result, {"object_count": 3})
        self.assertEqual(len(fake_socket.sent), 1)
        wire_payload = fake_socket.sent[0]
        self.assertTrue(wire_payload.endswith(b"\n"))
        self.assertEqual(wire_payload.count(b"\n"), 1)
        request = json.loads(wire_payload)
        self.assertEqual(request["type"], "get_scene_info")
        self.assertEqual(request["params"], {"limit": 25})
        self.assertIsInstance(request["id"], str)
        self.assertTrue(request["id"])
        self.assertEqual(fake_socket.connected_to, ("127.0.0.1", 19876))

    def test_malformed_json_response_closes_connection(self):
        fake_socket = ScriptedSocket(lambda _request: [b'{"status": nope}\n'])
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch, self.assertRaisesRegex(ConnectionError, "Malformed response"):
            connection.send_command("get_scene_info", {})

        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_non_object_response_is_rejected(self):
        fake_socket = ScriptedSocket(lambda _request: [b"[]\n"])
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch, self.assertRaisesRegex(ConnectionError, "Unexpected response shape"):
            connection.send_command("get_scene_info", {})

        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_oversized_unterminated_response_closes_connection(self):
        response_limit = 32
        fake_socket = ScriptedSocket(
            lambda _request: [b"x" * (response_limit + 1)]
        )
        connection, socket_patch = self.connect_with(fake_socket)

        with (
            socket_patch,
            mock.patch.object(server, "MAX_RESPONSE_BYTES", response_limit),
            self.assertRaisesRegex(ConnectionError, r"exceeds the 32-byte limit"),
        ):
            connection.send_command("get_scene_info", {})

        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_oversized_complete_response_line_closes_connection(self):
        response_limit = 32
        fake_socket = ScriptedSocket(
            lambda _request: [b"x" * (response_limit + 1) + b"\n"]
        )
        connection, socket_patch = self.connect_with(fake_socket)

        with (
            socket_patch,
            mock.patch.object(server, "MAX_RESPONSE_BYTES", response_limit),
            self.assertRaisesRegex(ConnectionError, r"exceeds the 32-byte limit"),
        ):
            connection.send_command("get_scene_info", {})

        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_mismatched_response_id_is_rejected_and_connection_closed(self):
        fake_socket = ScriptedSocket(
            lambda request: [response_bytes(request, response_id="a-different-request")]
        )
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch, self.assertRaises(ConnectionError) as raised:
            connection.send_command("get_scene_info", {})

        message = str(raised.exception).lower()
        self.assertIn("id", message)
        self.assertIn("mismatch", message)
        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_generic_addon_overload_error_is_surfaced_and_connection_closed(self):
        error_message = "Blender command queue is full; try again later"

        def overload_response(_request):
            response = {"id": None, "status": "error", "error": error_message}
            return [(json.dumps(response) + "\n").encode("utf-8")]

        fake_socket = ScriptedSocket(overload_response)
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch, self.assertRaisesRegex(RuntimeError, error_message):
            connection.send_command("get_scene_info", {})

        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_timeout_reports_unknown_outcome_and_does_not_retry(self):
        fake_socket = ScriptedSocket(
            lambda _request: [socket.timeout("scripted timeout")]
        )
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch, self.assertRaises(ConnectionError) as raised:
            connection.send_command("execute_code", {"code": "result = 1"}, timeout=0.25)

        message = str(raised.exception).lower()
        self.assertIn("timed out", message)
        self.assertIn("outcome", message)
        self.assertIn("unknown", message)
        self.assertTrue(
            "may still" in message or "not cancel" in message or "does not cancel" in message,
            f"timeout should explain that Blender work can continue: {message!r}",
        )
        self.assertEqual(len(fake_socket.sent), 1)
        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_partial_response_drip_cannot_extend_total_deadline(self):
        fake_socket = ScriptedSocket(
            lambda _request: [b'{', b'"', b"still-no-newline"]
        )
        connection, socket_patch = self.connect_with(fake_socket)
        monotonic_times = [0.0, 0.1, 0.2, 0.4, 0.7, 1.01]

        with (
            socket_patch,
            mock.patch.object(server.time, "monotonic", side_effect=monotonic_times) as clock,
            self.assertRaises(ConnectionError) as raised,
        ):
            connection.send_command("get_scene_info", {}, timeout=1.0)

        message = str(raised.exception).lower()
        self.assertIn("timed out after 1s", message)
        self.assertIn("outcome", message)
        self.assertIn("unknown", message)
        self.assertEqual(clock.call_count, len(monotonic_times))
        self.assertEqual(len(fake_socket.sent), 1)
        self.assertEqual(list(fake_socket.recv_items), [b"still-no-newline"])
        self.assertEqual(len(fake_socket.timeouts), 4)
        self.assertTrue(
            all(earlier > later for earlier, later in zip(fake_socket.timeouts, fake_socket.timeouts[1:]))
        )
        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_lock_wait_timeout_reports_that_no_command_was_sent(self):
        connection = server.BlenderConnection("127.0.0.1", 19876)
        lock = mock.Mock()
        lock.acquire.return_value = False
        connection._lock = lock

        with (
            mock.patch.object(server.socket, "socket") as socket_factory,
            self.assertRaises(ConnectionError) as raised,
        ):
            connection.send_command("delete_object", {"name": "Cube"}, timeout=0.25)

        message = str(raised.exception).lower()
        self.assertIn("timed out after 0.25s", message)
        self.assertIn("waiting for another blender command", message)
        self.assertIn("no command was sent", message)
        lock.acquire.assert_called_once_with(timeout=0.25)
        lock.release.assert_not_called()
        socket_factory.assert_not_called()

    def test_connect_timeout_reports_that_no_command_was_sent(self):
        fake_socket = ScriptedSocket()
        fake_socket.connect = mock.Mock(side_effect=socket.timeout("scripted connect timeout"))
        connection, socket_patch = self.connect_with(fake_socket)

        with socket_patch, self.assertRaises(ConnectionError) as raised:
            connection.send_command("delete_object", {"name": "Cube"}, timeout=0.25)

        message = str(raised.exception).lower()
        self.assertIn("timed out", message)
        self.assertIn("connecting to blender", message)
        self.assertIn("no command was sent", message)
        self.assertEqual(fake_socket.sent, [])
        self.assertTrue(fake_socket.closed)
        self.assertIsNone(connection.sock)

    def test_stale_reused_connection_reconnects_once(self):
        stale_socket = ScriptedSocket(send_error=BrokenPipeError("stale connection"))
        fresh_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"name": "Cube"})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = stale_socket

        with mock.patch.object(server.socket, "socket", return_value=fresh_socket) as socket_factory:
            result = connection.send_command("get_object_info", {"name": "Cube"})

        self.assertEqual(result, {"name": "Cube"})
        self.assertTrue(stale_socket.closed)
        self.assertIs(connection.sock, fresh_socket)
        socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(len(stale_socket.sent), 1)
        self.assertEqual(len(fresh_socket.sent), 1)

    def test_reused_connection_close_after_read_retries_with_same_request_id(self):
        closed_socket = ScriptedSocket(lambda _request: [b""])
        fresh_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"name": "Cube"})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = closed_socket

        with mock.patch.object(server.socket, "socket", return_value=fresh_socket) as socket_factory:
            result = connection.send_command("get_object_info", {"name": "Cube"})

        first_request = json.loads(closed_socket.sent[0])
        retried_request = json.loads(fresh_socket.sent[0])
        self.assertEqual(result, {"name": "Cube"})
        self.assertTrue(closed_socket.closed)
        self.assertIs(connection.sock, fresh_socket)
        socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(first_request, retried_request)
        self.assertIsInstance(first_request["id"], str)
        self.assertTrue(first_request["id"])

    def test_first_connection_close_after_read_retries_with_same_request_id(self):
        first_socket = ScriptedSocket(lambda _request: [b""])
        retry_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"object_count": 1})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)

        with mock.patch.object(
            server.socket, "socket", side_effect=[first_socket, retry_socket]
        ) as socket_factory:
            result = connection.send_command("get_scene_info", {"limit": 25})

        first_request = json.loads(first_socket.sent[0])
        retried_request = json.loads(retry_socket.sent[0])
        self.assertEqual(result, {"object_count": 1})
        self.assertTrue(first_socket.closed)
        self.assertIs(connection.sock, retry_socket)
        self.assertEqual(socket_factory.call_count, 2)
        self.assertEqual(first_request, retried_request)
        self.assertIsInstance(first_request["id"], str)
        self.assertTrue(first_request["id"])

    def test_partial_response_disconnect_retries_from_clean_buffer_with_same_id(self):
        def partial_response(request):
            response = response_bytes(request, result={"object_count": 1})
            return [response[:19], b""]

        first_socket = ScriptedSocket(partial_response)
        retry_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"object_count": 1})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)

        with mock.patch.object(
            server.socket, "socket", side_effect=[first_socket, retry_socket]
        ) as socket_factory:
            result = connection.send_command("get_scene_info", {"limit": 25})

        first_request = json.loads(first_socket.sent[0])
        retried_request = json.loads(retry_socket.sent[0])
        self.assertEqual(result, {"object_count": 1})
        self.assertTrue(first_socket.closed)
        self.assertIs(connection.sock, retry_socket)
        self.assertEqual(connection.buf, b"")
        self.assertEqual(socket_factory.call_count, 2)
        self.assertEqual(first_request, retried_request)
        self.assertIsInstance(first_request["id"], str)
        self.assertTrue(first_request["id"])

    def test_mutating_close_after_send_does_not_retry_and_reports_unknown_outcome(self):
        closed_socket = ScriptedSocket(lambda _request: [b""])
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = closed_socket

        with (
            mock.patch.object(server.socket, "socket") as socket_factory,
            self.assertRaises(ConnectionError) as raised,
        ):
            connection.send_command("delete_object", {"name": "Cube"})

        message = str(raised.exception).lower()
        self.assertIn("outcome", message)
        self.assertIn("unknown", message)
        self.assertIn("may still complete", message)
        self.assertEqual(len(closed_socket.sent), 1)
        self.assertEqual(json.loads(closed_socket.sent[0])["type"], "delete_object")
        self.assertTrue(closed_socket.closed)
        self.assertIsNone(connection.sock)
        socket_factory.assert_not_called()

    def test_reused_connection_timeout_after_send_does_not_retry(self):
        timed_out_socket = ScriptedSocket(
            lambda _request: [socket.timeout("scripted response timeout")]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = timed_out_socket

        with (
            mock.patch.object(server.socket, "socket") as socket_factory,
            self.assertRaises(ConnectionError) as raised,
        ):
            connection.send_command("delete_object", {"name": "Cube"}, timeout=0.25)

        message = str(raised.exception).lower()
        self.assertIn("outcome", message)
        self.assertIn("unknown", message)
        self.assertEqual(len(timed_out_socket.sent), 1)
        self.assertTrue(timed_out_socket.closed)
        self.assertIsNone(connection.sock)
        socket_factory.assert_not_called()

    def test_retry_reuses_the_same_request_id(self):
        stale_socket = ScriptedSocket(send_error=BrokenPipeError("stale connection"))
        fresh_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"object_count": 1})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = stale_socket

        with mock.patch.object(server.socket, "socket", return_value=fresh_socket):
            result = connection.send_command("get_scene_info", {"limit": 25})

        first_request = json.loads(stale_socket.sent[0])
        retried_request = json.loads(fresh_socket.sent[0])
        self.assertEqual(result, {"object_count": 1})
        self.assertIsInstance(first_request["id"], str)
        self.assertTrue(first_request["id"])
        self.assertEqual(first_request["id"], retried_request["id"])
        self.assertEqual(first_request, retried_request)

    def test_new_introspection_command_is_retry_safe(self):
        # get_objects_summary is one of the newer read-only introspection
        # tools; it must follow the same retry-after-close-on-read path as
        # get_scene_info/get_object_info, driven by RETRY_SAFE_COMMANDS.
        self.assertIn("get_objects_summary", server.RETRY_SAFE_COMMANDS)
        closed_socket = ScriptedSocket(lambda _request: [b""])
        fresh_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"scene_name": "Scene"})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = closed_socket

        with mock.patch.object(server.socket, "socket", return_value=fresh_socket):
            result = connection.send_command("get_objects_summary", {})

        self.assertEqual(result, {"scene_name": "Scene"})
        self.assertTrue(closed_socket.closed)
        self.assertIs(connection.sock, fresh_socket)

    def test_character_proportion_audit_is_retry_safe(self):
        self.assertIn("analyze_character_proportions", server.RETRY_SAFE_COMMANDS)
        closed_socket = ScriptedSocket(lambda _request: [b""])
        fresh_socket = ScriptedSocket(
            lambda request: [response_bytes(request, result={"height": 1.8})]
        )
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = closed_socket

        with mock.patch.object(server.socket, "socket", return_value=fresh_socket):
            result = connection.send_command(
                "analyze_character_proportions", {"collection_name": "Character"}
            )

        self.assertEqual(result, {"height": 1.8})
        self.assertTrue(closed_socket.closed)
        self.assertIs(connection.sock, fresh_socket)

    def test_new_render_command_is_not_retry_safe(self):
        # render_thumbnail writes a file as a side effect, like render_scene;
        # it must never be silently replayed after losing its connection.
        self.assertNotIn("render_thumbnail", server.RETRY_SAFE_COMMANDS)
        closed_socket = ScriptedSocket(lambda _request: [b""])
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = closed_socket

        with (
            mock.patch.object(server.socket, "socket") as socket_factory,
            self.assertRaises(ConnectionError) as raised,
        ):
            connection.send_command("render_thumbnail", {"size": 32})

        message = str(raised.exception).lower()
        self.assertIn("outcome", message)
        self.assertIn("unknown", message)
        socket_factory.assert_not_called()

    def test_turntable_review_is_not_retry_safe(self):
        self.assertNotIn("render_turntable_review", server.RETRY_SAFE_COMMANDS)
        closed_socket = ScriptedSocket(lambda _request: [b""])
        connection = server.BlenderConnection("127.0.0.1", 19876)
        connection.sock = closed_socket

        with (
            mock.patch.object(server.socket, "socket") as socket_factory,
            self.assertRaises(ConnectionError),
        ):
            connection.send_command(
                "render_turntable_review", {"collection_name": "Character", "size": 128}
            )
        socket_factory.assert_not_called()

    def test_server_exposes_field_guide_instructions(self):
        self.assertEqual(server.mcp.instructions, server.SERVER_INSTRUCTIONS)
        self.assertIn("rotation_mode", server.SERVER_INSTRUCTIONS)
        self.assertIn("_for_cli", server.SERVER_INSTRUCTIONS)
        self.assertIn("Asset-creation quality workflow", server.SERVER_INSTRUCTIONS)
        self.assertIn("Blockout", server.SERVER_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
