"""Headless regression checks for the Blender-side MCP add-on.

Run from the repository root with::

    blender --background --factory-startup --python-exit-code 1 \
        --python addon/tests/headless_regression.py

The add-on module is imported directly and is deliberately not registered, so
these tests never start its TCP server or install its timer.
"""

from __future__ import annotations

from collections import OrderedDict
import importlib.util
import math
from pathlib import Path
import queue
import sys
from types import SimpleNamespace
import unittest

import bmesh
import bpy
from mathutils import Vector


ADDON_PATH = Path(__file__).resolve().parents[1] / "blender_mcp_addon.py"


def _load_addon():
    spec = importlib.util.spec_from_file_location(
        "blender_mcp_addon_headless_test", ADDON_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load add-on module from {ADDON_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADDON = _load_addon()


def _remove_all_objects() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.view_layer.objects.active = None


def _mesh_component_count(mesh: bpy.types.Mesh) -> int:
    neighbors = [set() for _ in mesh.vertices]
    for edge in mesh.edges:
        left, right = edge.vertices
        neighbors[left].add(right)
        neighbors[right].add(left)

    unseen = set(range(len(mesh.vertices)))
    components = 0
    while unseen:
        components += 1
        pending = [unseen.pop()]
        while pending:
            vertex = pending.pop()
            adjacent = neighbors[vertex] & unseen
            unseen.difference_update(adjacent)
            pending.extend(adjacent)
    return components


def _mesh_vertex_components(mesh: bpy.types.Mesh) -> list[set[int]]:
    neighbors = [set() for _ in mesh.vertices]
    for edge in mesh.edges:
        left, right = edge.vertices
        neighbors[left].add(right)
        neighbors[right].add(left)

    unseen = set(range(len(mesh.vertices)))
    components = []
    while unseen:
        component = {unseen.pop()}
        pending = list(component)
        while pending:
            vertex = pending.pop()
            adjacent = neighbors[vertex] & unseen
            unseen.difference_update(adjacent)
            component.update(adjacent)
            pending.extend(adjacent)
        components.append(component)
    return components


class BlenderMCPHeadlessTests(unittest.TestCase):
    def setUp(self) -> None:
        _remove_all_objects()
        # The imported test module never starts the listener, but queue tests
        # still replace every mutable protocol global. Saving the original
        # objects (rather than merely clearing them) makes teardown safe even
        # if this file is run from an already-imported test environment.
        self._protocol_globals = {
            "_command_queue": ADDON._command_queue,
            "_response_cache": ADDON._response_cache,
            "_response_cache_bytes": ADDON._response_cache_bytes,
            "_HANDLERS": ADDON._HANDLERS,
            "_MUTATING_COMMANDS": ADDON._MUTATING_COMMANDS,
            "_generation": ADDON._generation,
            "_running": ADDON._running,
            "_server_socket": ADDON._server_socket,
            "_server_thread": ADDON._server_thread,
            "_bound_port": ADDON._bound_port,
            "_timer_registered": ADDON._timer_registered,
            "_last_server_error": ADDON._last_server_error,
            "_client_sockets": ADDON._client_sockets,
        }
        ADDON._command_queue = queue.Queue(maxsize=ADDON.MAX_PENDING_COMMANDS)
        ADDON._response_cache = OrderedDict()
        ADDON._response_cache_bytes = 0
        ADDON._HANDLERS = {}
        ADDON._MUTATING_COMMANDS = set()
        ADDON._generation = 42
        ADDON._running = True
        ADDON._server_socket = None
        ADDON._server_thread = None
        ADDON._bound_port = None
        ADDON._timer_registered = False
        ADDON._last_server_error = None
        ADDON._client_sockets = {}

    def tearDown(self) -> None:
        # A test may temporarily replace the add-on's bpy reference.
        ADDON.bpy = bpy
        for name, value in self._protocol_globals.items():
            setattr(ADDON, name, value)
        _remove_all_objects()

    def process_queued_command(self, command, *, generation=None):
        response_box = queue.Queue(maxsize=1)
        ADDON._command_queue.put_nowait(
            (
                command,
                response_box,
                ADDON._generation if generation is None else generation,
            )
        )
        try:
            next_interval = ADDON._process_queue()
        except BaseException as exc:  # turn timer-killing exceptions into a test failure
            self.fail(f"_process_queue leaked {type(exc).__name__}: {exc}")
        self.assertFalse(response_box.empty(), "queued command produced no response")
        return response_box.get_nowait(), next_interval

    def assert_closed_connected_mesh(self, obj: bpy.types.Object) -> None:
        self.assertEqual(obj.type, "MESH")
        mesh = obj.data
        self.assertGreater(len(mesh.vertices), 0)
        self.assertGreater(len(mesh.polygons), 0)
        self.assertEqual(
            _mesh_component_count(mesh),
            1,
            "mesh contains disconnected shells (a joined object is not necessarily unified)",
        )

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            non_manifold = [edge.index for edge in bm.edges if not edge.is_manifold]
            self.assertEqual(non_manifold, [], "mesh has boundary/non-manifold edges")
            self.assertTrue(
                all(face.calc_area() > 1.0e-12 for face in bm.faces),
                "mesh contains degenerate faces",
            )
            self.assertEqual(
                len(bm.verts) - len(bm.edges) + len(bm.faces),
                2,
                "expected a single closed genus-zero surface",
            )
            self.assertGreater(
                bm.calc_volume(signed=True),
                0.0,
                "face winding should produce a positive signed volume",
            )
        finally:
            bm.free()

    def test_capsule_is_one_closed_connected_mesh(self) -> None:
        sentinel = bpy.data.objects.new("SelectionSentinel", None)
        bpy.context.scene.collection.objects.link(sentinel)
        sentinel.select_set(True)
        bpy.context.view_layer.objects.active = sentinel

        start = Vector((-1.25, 0.5, 2.0))
        end = Vector((2.75, 3.5, 6.0))
        radius = 0.375
        summary = ADDON.cmd_add_capsule(
            {
                "start": list(start),
                "end": list(end),
                "radius": radius,
                "caps": True,
                "name": "RegressionCapsule",
            }
        )

        self.assertEqual(summary["name"], "RegressionCapsule")
        self.assertEqual(len(bpy.context.scene.objects), 2)
        capsule = bpy.context.scene.objects.get("RegressionCapsule")
        self.assertIsNotNone(capsule)
        self.assertIsNone(bpy.context.scene.objects.get("RegressionCapsule_cap_start"))
        self.assertIsNone(bpy.context.scene.objects.get("RegressionCapsule_cap_end"))
        self.assert_closed_connected_mesh(capsule)

        # Verify the silhouette in world space. This also catches an object
        # whose geometry and transform both apply the axis rotation.
        axis_delta = end - start
        axis = axis_delta.normalized()
        axial = []
        radial = []
        for vertex in capsule.data.vertices:
            offset = (capsule.matrix_world @ vertex.co) - start
            distance_along_axis = offset.dot(axis)
            axial.append(distance_along_axis)
            radial.append((offset - axis * distance_along_axis).length)
        self.assertAlmostEqual(min(axial), -radius, delta=1.0e-4)
        self.assertAlmostEqual(max(axial), axis_delta.length + radius, delta=1.0e-4)
        self.assertAlmostEqual(max(radial), radius, delta=1.0e-4)

        # Creating a capsule must not disturb the user's previous selection.
        self.assertIs(bpy.context.view_layer.objects.active, sentinel)
        self.assertEqual(set(bpy.context.selected_objects), {sentinel})

    def test_capsule_without_rounded_caps_is_still_closed(self) -> None:
        start = Vector((0.0, 0.0, -1.5))
        end = Vector((0.0, 0.0, 2.0))
        radius = 0.25
        ADDON.cmd_add_capsule(
            {
                "start": list(start),
                "end": list(end),
                "radius": radius,
                "caps": False,
                "name": "RegressionCylinder",
            }
        )
        cylinder = bpy.context.scene.objects.get("RegressionCylinder")
        self.assertIsNotNone(cylinder)
        self.assertEqual(len(bpy.context.scene.objects), 1)
        self.assert_closed_connected_mesh(cylinder)

        world_z = [
            (cylinder.matrix_world @ vertex.co).z
            for vertex in cylinder.data.vertices
        ]
        self.assertAlmostEqual(min(world_z), start.z, delta=1.0e-5)
        self.assertAlmostEqual(max(world_z), end.z, delta=1.0e-5)

    def test_capsule_rejects_invalid_input_before_creating_objects(self) -> None:
        base = {
            "start": [0.0, 0.0, 0.0],
            "end": [0.0, 0.0, 2.0],
            "radius": 0.25,
            "caps": True,
            "name": "MustNotExist",
        }
        cases = {
            "zero radius": {"radius": 0.0},
            "negative radius": {"radius": -0.25},
            "non-finite radius": {"radius": math.nan},
            "coincident endpoints": {"end": [0.0, 0.0, 0.0]},
            "wrong start arity": {"start": [0.0, 0.0]},
            "non-boolean caps": {"caps": "yes"},
        }
        for label, replacement in cases.items():
            with self.subTest(label=label):
                params = dict(base)
                params.update(replacement)
                before = set(bpy.context.scene.objects)
                with self.assertRaises((TypeError, ValueError, OverflowError)):
                    ADDON.cmd_add_capsule(params)
                leaked = set(bpy.context.scene.objects) - before
                for obj in leaked:
                    bpy.data.objects.remove(obj, do_unlink=True)
                self.assertEqual(leaked, set(), "validation happened after scene mutation")

    def test_primitive_cancelled_after_creation_restores_state_without_mesh_leak(self) -> None:
        sentinel = bpy.data.objects.new("PrimitiveSelectionSentinel", None)
        bpy.context.scene.collection.objects.link(sentinel)
        sentinel.select_set(True)
        bpy.context.view_layer.objects.active = sentinel
        objects_before = set(bpy.data.objects)
        meshes_before = set(bpy.data.meshes)
        created_mesh_name = None
        original_cube_operator = ADDON._PRIMITIVES["cube"]

        def create_then_cancel(**kwargs):
            nonlocal created_mesh_name
            actual_result = bpy.ops.mesh.primitive_cube_add(**kwargs)
            self.assertIn("FINISHED", actual_result)
            created = bpy.context.active_object
            self.assertIsNotNone(created)
            created_mesh_name = created.data.name
            return {"CANCELLED"}

        ADDON._PRIMITIVES["cube"] = create_then_cancel
        try:
            with self.assertRaisesRegex(
                RuntimeError, r"Failed to add cube .*CANCELLED"
            ):
                ADDON.cmd_add_primitive(
                    {
                        "type": "cube",
                        "name": "MustBeRolledBack",
                        "location": [1.0, 2.0, 3.0],
                    }
                )
        finally:
            ADDON._PRIMITIVES["cube"] = original_cube_operator

        self.assertIsNotNone(created_mesh_name)
        self.assertEqual(set(bpy.data.objects), objects_before)
        self.assertEqual(set(bpy.data.meshes), meshes_before)
        self.assertIsNone(bpy.data.meshes.get(created_mesh_name))
        self.assertIs(bpy.context.view_layer.objects.active, sentinel)
        self.assertEqual(set(bpy.context.selected_objects), {sentinel})

    def test_mirror_then_join_preserves_outward_winding_and_removes_source_mesh(self) -> None:
        ADDON.cmd_add_primitive(
            {
                "type": "cube",
                "name": "OriginalCube",
                "location": [3.0, 0.25, -0.5],
                "rotation": [0.2, -0.35, 0.4],
                "scale": [0.5, 0.75, 1.25],
            }
        )
        ADDON.cmd_mirror_object(
            {
                "name": "OriginalCube",
                "axis": "X",
                "new_name": "MirroredCube",
            }
        )
        mirrored = bpy.context.scene.objects.get("MirroredCube")
        self.assertIsNotNone(mirrored)
        source_mesh_name = mirrored.data.name
        self.assertEqual(mirrored.data.users, 1)

        ADDON.cmd_join_objects(
            {
                "names": ["OriginalCube", "MirroredCube"],
                "target_name": "JoinedMirrorPair",
            }
        )
        joined = bpy.context.scene.objects.get("JoinedMirrorPair")
        self.assertIsNotNone(joined)
        self.assertEqual(len(bpy.context.scene.objects), 1)
        self.assertIsNone(
            bpy.data.meshes.get(source_mesh_name),
            "join left the consumed source mesh as a zero-user datablock",
        )

        mesh = joined.data
        components = _mesh_vertex_components(mesh)
        self.assertEqual(len(components), 2)
        world_vertices = [joined.matrix_world @ vertex.co for vertex in mesh.vertices]
        component_for_vertex = {
            vertex_index: component_index
            for component_index, component in enumerate(components)
            for vertex_index in component
        }
        centroids = [
            sum((world_vertices[index] for index in component), Vector()) / len(component)
            for component in components
        ]
        signed_volumes = [0.0 for _component in components]

        for polygon in mesh.polygons:
            component_index = component_for_vertex[polygon.vertices[0]]
            self.assertTrue(
                all(component_for_vertex[index] == component_index for index in polygon.vertices),
                "a face unexpectedly bridges the two disconnected cubes",
            )
            points = [world_vertices[index] for index in polygon.vertices]
            face_center = sum(points, Vector()) / len(points)
            geometric_normal = (points[1] - points[0]).cross(points[2] - points[0])
            self.assertGreater(
                geometric_normal.dot(face_center - centroids[component_index]),
                0.0,
                "joined cube has an inward-wound face",
            )
            for index in range(1, len(points) - 1):
                signed_volumes[component_index] += (
                    points[0].dot(points[index].cross(points[index + 1])) / 6.0
                )

        self.assertTrue(
            all(volume > 0.0 for volume in signed_volumes),
            f"each disconnected component must retain positive signed volume: {signed_volumes}",
        )
        self.assertLess(centroids[0].x * centroids[1].x, 0.0)

    def test_scene_info_limit_and_validation(self) -> None:
        for index in range(4):
            obj = bpy.data.objects.new(f"InfoObject{index}", None)
            bpy.context.scene.collection.objects.link(obj)

        info = ADDON.cmd_get_scene_info({"limit": 2})
        self.assertEqual(info["object_count"], 4)
        self.assertTrue(info["truncated"])
        self.assertEqual(len(info["objects"]), 2)

        for invalid in (0, -1, 1.5, True, 1_000_000_000):
            with self.subTest(limit=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    ADDON.cmd_get_scene_info({"limit": invalid})

    def test_start_server_permission_error_leaves_lifecycle_clean(self) -> None:
        generation_before = ADDON._generation
        ADDON._running = False
        original_socket_factory = ADDON.socket.socket

        def denied_socket(*_args, **_kwargs):
            raise PermissionError("listener creation denied")

        ADDON.socket.socket = denied_socket
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                r"Could not listen .*listener creation denied",
            ):
                ADDON.start_server(19876)
        finally:
            ADDON.socket.socket = original_socket_factory

        self.assertIsNone(ADDON._server_socket)
        self.assertIsNone(ADDON._server_thread)
        self.assertFalse(ADDON._running)
        self.assertIsNone(ADDON._bound_port)
        self.assertFalse(ADDON._timer_registered)
        self.assertEqual(ADDON._generation, generation_before)
        self.assertIsNone(ADDON._last_server_error)
        self.assertEqual(ADDON._client_sockets, {})

    def test_old_generation_cleanup_preserves_new_generation_client(self) -> None:
        class TrackedSocket:
            def __init__(self, label):
                self.label = label
                self.shutdown_calls = []
                self.close_calls = 0

            def shutdown(self, how):
                self.shutdown_calls.append(how)

            def close(self):
                self.close_calls += 1

        old_generation = ADDON._generation - 1
        old_client = TrackedSocket("old")
        new_client = TrackedSocket("new")
        ADDON._client_sockets = {
            old_client: old_generation,
            new_client: ADDON._generation,
        }

        closed_count = ADDON._close_client_sockets(old_generation)

        self.assertEqual(closed_count, 1)
        self.assertEqual(old_client.shutdown_calls, [ADDON.socket.SHUT_RDWR])
        self.assertEqual(old_client.close_calls, 1)
        self.assertEqual(new_client.shutdown_calls, [])
        self.assertEqual(new_client.close_calls, 0)
        self.assertEqual(
            ADDON._client_sockets,
            {new_client: ADDON._generation},
            "old listener cleanup removed a client owned by the new generation",
        )

    def test_render_limits_are_rejected_before_rendering(self) -> None:
        # There is intentionally no active camera. Oversized inputs must be
        # rejected as validation errors before the handler reaches that check
        # (and, critically, before any huge image allocation can occur).
        cases = (
            {"resolution_x": 1_000_000, "resolution_y": 64, "samples": 1},
            {"resolution_x": 64, "resolution_y": 1_000_000, "samples": 1},
            {"resolution_x": 64, "resolution_y": 64, "samples": 1_000_000_000},
            {"resolution_x": 64.5, "resolution_y": 64, "samples": 1},
            {"resolution_x": 64, "resolution_y": 64, "samples": True},
        )
        for params in cases:
            with self.subTest(params=params):
                with self.assertRaises((TypeError, ValueError)):
                    ADDON.cmd_render_scene(params)

    def test_save_file_checks_both_operator_results(self) -> None:
        real_bpy = ADDON.bpy
        try:
            for label, current_path, params, expected_operator in (
                ("save as", "", {"filepath": "/tmp/mcp-never-written.blend"}, "save_as"),
                ("save existing", "/tmp/existing.blend", {}, "save"),
            ):
                with self.subTest(label=label):
                    calls = []

                    def save_as_mainfile(**kwargs):
                        calls.append(("save_as", kwargs))
                        return {"CANCELLED"}

                    def save_mainfile(**kwargs):
                        calls.append(("save", kwargs))
                        return {"CANCELLED"}

                    ADDON.bpy = SimpleNamespace(
                        data=SimpleNamespace(filepath=current_path),
                        ops=SimpleNamespace(
                            wm=SimpleNamespace(
                                save_as_mainfile=save_as_mainfile,
                                save_mainfile=save_mainfile,
                            )
                        ),
                        path=SimpleNamespace(abspath=lambda path: path),
                    )
                    with self.assertRaises(RuntimeError):
                        ADDON.cmd_save_file(params)
                    self.assertEqual([call[0] for call in calls], [expected_operator])
        finally:
            ADDON.bpy = real_bpy

    def test_process_queue_deduplicates_same_id_and_payload(self) -> None:
        calls = []

        def handler(params):
            calls.append(dict(params))
            return {"execution_count": len(calls), "value": params["value"]}

        ADDON._HANDLERS["test_probe"] = handler
        first_command = {
            "id": "same-request",
            "type": "test_probe",
            "params": {"value": 7, "metadata": {"left": 1, "right": 2}},
        }
        # Reordering mapping keys must not change the canonical fingerprint.
        replayed_command = {
            "params": {"metadata": {"right": 2, "left": 1}, "value": 7},
            "type": "test_probe",
            "id": "same-request",
        }

        first, first_interval = self.process_queued_command(first_command)
        replayed, replayed_interval = self.process_queued_command(replayed_command)

        self.assertEqual(len(calls), 1)
        self.assertEqual(replayed, first)
        self.assertEqual(first["id"], "same-request")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["result"]["execution_count"], 1)
        self.assertEqual(first_interval, 0.05)
        self.assertEqual(replayed_interval, 0.05)
        self.assertEqual(len(ADDON._response_cache), 1)
        self.assertGreater(ADDON._response_cache_bytes, 0)

    def test_process_queue_rejects_id_reuse_with_different_payload(self) -> None:
        calls = []

        def handler(params):
            calls.append(params["value"])
            return {"value": params["value"]}

        ADDON._HANDLERS["test_probe"] = handler
        original = {
            "id": "reused-request",
            "type": "test_probe",
            "params": {"value": "original"},
        }
        conflicting = {
            "id": "reused-request",
            "type": "test_probe",
            "params": {"value": "different"},
        }

        success, _interval = self.process_queued_command(original)
        error, _interval = self.process_queued_command(conflicting)
        replayed, _interval = self.process_queued_command(original)

        self.assertEqual(calls, ["original"])
        self.assertEqual(success["status"], "ok")
        self.assertEqual(error["id"], "reused-request")
        self.assertEqual(error["status"], "error")
        self.assertIn("different command", error["error"].lower())
        self.assertEqual(replayed, success, "a conflict must not replace the cached response")
        self.assertEqual(len(ADDON._response_cache), 1)

    def test_process_queue_rejects_stale_generation_without_invoking_handler(self) -> None:
        calls = []

        def handler(params):
            calls.append(params)
            return {"unexpected": True}

        ADDON._HANDLERS["test_probe"] = handler
        command = {
            "id": "stale-request",
            "type": "test_probe",
            "params": {"value": 1},
        }
        response, interval = self.process_queued_command(
            command, generation=ADDON._generation - 1
        )

        self.assertEqual(calls, [])
        self.assertEqual(response["id"], "stale-request")
        self.assertEqual(response["status"], "error")
        self.assertIn("generation", response["error"].lower())
        self.assertEqual(interval, 0.05)
        self.assertEqual(len(ADDON._response_cache), 0)
        self.assertEqual(ADDON._response_cache_bytes, 0)

    def test_process_queue_serializes_keyboard_interrupt(self) -> None:
        calls = []

        def handler(_params):
            calls.append("called")
            raise KeyboardInterrupt("scripted interrupt")

        ADDON._HANDLERS["test_interrupt"] = handler
        command = {
            "id": "interrupt-request",
            "type": "test_interrupt",
            "params": {},
        }

        response, interval = self.process_queued_command(command)
        replayed, replayed_interval = self.process_queued_command(dict(command))

        self.assertEqual(calls, ["called"])
        self.assertEqual(response["id"], "interrupt-request")
        self.assertEqual(response["status"], "error")
        self.assertIn("scripted interrupt", response["error"])
        self.assertIn("KeyboardInterrupt", response["traceback"])
        self.assertEqual(replayed, response)
        self.assertEqual(interval, 0.05)
        self.assertEqual(replayed_interval, 0.05)

    def test_process_queue_contains_exception_with_hostile_string_conversion(self) -> None:
        class HostileBaseException(BaseException):
            def __str__(self):
                raise RuntimeError("hostile __str__ was invoked")

        calls = []

        def handler(_params):
            calls.append("called")
            raise HostileBaseException()

        ADDON._HANDLERS["test_hostile_exception"] = handler
        command = {
            "id": "hostile-exception-request",
            "type": "test_hostile_exception",
            "params": {},
        }

        response, interval = self.process_queued_command(command)

        self.assertEqual(calls, ["called"])
        self.assertEqual(response["id"], "hostile-exception-request")
        self.assertEqual(response["status"], "error")
        self.assertIsInstance(response["error"], str)
        self.assertTrue(response["error"])
        self.assertEqual(interval, 0.05)

    def test_mutating_queue_command_pushes_one_undo_boundary_and_replay_pushes_none(self) -> None:
        handler_calls = []
        undo_calls = []

        def handler(params):
            handler_calls.append(dict(params))
            return {"mutated": True}

        def undo_push(**kwargs):
            undo_calls.append(kwargs)
            return {"FINISHED"}

        ADDON._HANDLERS["test_mutating"] = handler
        ADDON._MUTATING_COMMANDS.add("test_mutating")
        ADDON.bpy = SimpleNamespace(
            ops=SimpleNamespace(ed=SimpleNamespace(undo_push=undo_push))
        )
        command = {
            "id": "successful-mutation",
            "type": "test_mutating",
            "params": {"value": 3},
        }

        response, _interval = self.process_queued_command(command)
        replayed, _interval = self.process_queued_command(dict(command))

        self.assertEqual(handler_calls, [{"value": 3}])
        self.assertEqual(undo_calls, [{"message": "MCP: test_mutating"}])
        self.assertEqual(response["status"], "ok")
        self.assertEqual(replayed, response)

    def test_failed_execute_code_still_pushes_one_undo_boundary(self) -> None:
        handler_calls = []
        undo_calls = []

        def failing_handler(params):
            handler_calls.append(dict(params))
            raise RuntimeError("script failed after mutating")

        def undo_push(**kwargs):
            undo_calls.append(kwargs)
            return {"FINISHED"}

        ADDON._HANDLERS["execute_code"] = failing_handler
        ADDON.bpy = SimpleNamespace(
            ops=SimpleNamespace(ed=SimpleNamespace(undo_push=undo_push))
        )
        command = {
            "id": "failed-execute-code",
            "type": "execute_code",
            "params": {"code": "partially_mutate_then_fail()"},
        }

        response, _interval = self.process_queued_command(command)
        replayed, _interval = self.process_queued_command(dict(command))

        self.assertEqual(
            handler_calls, [{"code": "partially_mutate_then_fail()"}]
        )
        self.assertEqual(undo_calls, [{"message": "MCP: execute_code"}])
        self.assertEqual(response["status"], "error")
        self.assertIn("script failed after mutating", response["error"])
        self.assertEqual(replayed, response)

    def test_cancelled_undo_push_is_cached_error_without_command_replay(self) -> None:
        handler_calls = []
        undo_calls = []

        def handler(params):
            handler_calls.append(dict(params))
            return {"mutation_completed": True}

        def cancelled_undo_push(**kwargs):
            undo_calls.append(kwargs)
            return {"CANCELLED"}

        ADDON._HANDLERS["test_mutating"] = handler
        ADDON._MUTATING_COMMANDS.add("test_mutating")
        ADDON.bpy = SimpleNamespace(
            ops=SimpleNamespace(ed=SimpleNamespace(undo_push=cancelled_undo_push))
        )
        command = {
            "id": "cancelled-undo-boundary",
            "type": "test_mutating",
            "params": {"value": 9},
        }

        response, _interval = self.process_queued_command(command)
        replayed, _interval = self.process_queued_command(dict(command))

        self.assertEqual(handler_calls, [{"value": 9}])
        self.assertEqual(undo_calls, [{"message": "MCP: test_mutating"}])
        self.assertEqual(response["status"], "error")
        self.assertIn("undo boundary", response["error"].lower())
        self.assertIn("cancelled", response["error"].lower())
        self.assertEqual(replayed, response)

    def test_bounded_response_drops_oversized_id_to_honor_wire_limit(self) -> None:
        original_limit = ADDON.MAX_RESPONSE_BYTES
        oversized_id = "request-" + ("x" * 1_000)
        try:
            ADDON.MAX_RESPONSE_BYTES = 100
            response, wire_payload = ADDON._bounded_response(
                {
                    "id": oversized_id,
                    "status": "ok",
                    "result": {"payload": "y" * 1_000},
                }
            )
        finally:
            ADDON.MAX_RESPONSE_BYTES = original_limit

        payload_bytes = wire_payload.encode("utf-8")
        self.assertLessEqual(len(payload_bytes), 100)
        self.assertEqual(ADDON.json.loads(wire_payload), response)
        self.assertEqual(response["status"], "error")
        self.assertNotEqual(response.get("id"), oversized_id)


def main() -> None:
    print(f"Running Blender MCP add-on tests with Blender {bpy.app.version_string}")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BlenderMCPHeadlessTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError(
            f"Blender MCP headless regressions failed: "
            f"{len(result.failures)} failure(s), {len(result.errors)} error(s)"
        )


if __name__ == "__main__":
    main()
