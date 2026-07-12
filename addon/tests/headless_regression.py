"""Headless regression checks for the Blender-side MCP add-on.

Run from the repository root with::

    blender --background --factory-startup --python-exit-code 1 \
        --python addon/tests/headless_regression.py

The add-on module is imported directly and is deliberately not registered, so
these tests never start its TCP server or install its timer.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

import bmesh
import bpy
from mathutils import Vector


ADDON_PATH = Path(__file__).resolve().parents[1] / "blender_mcp_addon.py"
RUNNER_PATH = Path(__file__).resolve().parents[1] / "blender_mcp_cli_runner.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_addon():
    return _load_module(ADDON_PATH, "blender_mcp_addon_headless_test")


ADDON = _load_addon()
RUNNER = _load_module(RUNNER_PATH, "blender_mcp_cli_runner_headless_test")


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
            "_online_access_allowed": ADDON._online_access_allowed,
            "_running_in_background": ADDON._running_in_background,
            "_idle_countdown": ADDON._idle_countdown,
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
        ADDON._idle_countdown = 0
        # --factory-startup defaults bpy.app.online_access to False, which
        # would otherwise fail every test that calls the real start_server();
        # only test_online_access_blocks_start_server exercises that path.
        ADDON._online_access_allowed = lambda: True
        # This entire suite runs under `blender --background`, which would
        # otherwise fail every test that calls the real start_server(); only
        # test_start_server_blocks_in_background_mode exercises that path.
        ADDON._running_in_background = lambda: False

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

    def run_cli_command(self, command_type, params):
        """Exercise blender_mcp_cli_runner.run() in-process, the same way a
        real `blender --background --python blender_mcp_cli_runner.py --
        ...` subprocess would, without spawning a nested Blender."""
        output_path = os.path.join(
            tempfile.gettempdir(), f"mcp_cli_runner_test_{uuid.uuid4().hex}.json"
        )
        params_b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        # setUp() empties ADDON._HANDLERS for the queue tests' isolation; the
        # runner needs the real dispatch table, restored here for its call only.
        emptied_handlers = ADDON._HANDLERS
        ADDON._HANDLERS = self._protocol_globals["_HANDLERS"]
        try:
            RUNNER.run(ADDON, command_type, params_b64, output_path)
            with open(output_path, "r", encoding="utf-8") as f:
                return json.load(f)
        finally:
            ADDON._HANDLERS = emptied_handlers
            try:
                os.remove(output_path)
            except OSError:
                pass

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

    def test_get_object_info_reports_children_constraints_data_name_and_collections(
        self,
    ) -> None:
        parent = bpy.data.objects.new("InfoParent", None)
        bpy.context.scene.collection.objects.link(parent)
        bpy.ops.mesh.primitive_cube_add()
        child = bpy.context.active_object
        child.parent = parent
        child.constraints.new(type="COPY_LOCATION")
        extra_collection = bpy.data.collections.new("InfoExtraCollection")
        bpy.context.scene.collection.children.link(extra_collection)
        extra_collection.objects.link(child)

        child_info = ADDON.cmd_get_object_info({"name": child.name})
        self.assertEqual(child_info["data_name"], child.data.name)
        self.assertEqual(child_info["children"], [])
        self.assertEqual(
            child_info["constraints"], [{"name": "Copy Location", "type": "COPY_LOCATION"}]
        )
        self.assertEqual(sorted(child_info["collections"]), ["Collection", "InfoExtraCollection"])

        parent_info = ADDON.cmd_get_object_info({"name": "InfoParent"})
        self.assertEqual(parent_info["children"], [child.name])
        self.assertIsNone(parent_info["data_name"])

    def test_get_object_info_world_bounding_box_accounts_for_rotation(self) -> None:
        bpy.ops.mesh.primitive_cube_add(size=2.0, location=(10.0, 0.0, 0.0))
        cube = bpy.context.active_object
        cube.rotation_euler = (0.0, 0.0, math.radians(45.0))
        bpy.context.view_layer.update()

        info = ADDON.cmd_get_object_info({"name": cube.name})
        min_corner, max_corner = info["world_bounding_box"]
        # An unrotated 2x2x2 cube at (10,0,0) would have an AABB half-extent
        # of 1.0 on every axis; a 45-degree rotation about Z grows the X/Y
        # half-extent to sqrt(2) while Z is untouched, which is exactly what
        # distinguishes this from the local-space `dimensions` field.
        self.assertAlmostEqual(max_corner[0] - min_corner[0], 2 * math.sqrt(2), places=5)
        self.assertAlmostEqual(max_corner[1] - min_corner[1], 2 * math.sqrt(2), places=5)
        self.assertAlmostEqual(max_corner[2] - min_corner[2], 2.0, places=5)
        self.assertAlmostEqual((min_corner[0] + max_corner[0]) / 2, 10.0, places=5)

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

    def test_online_access_blocks_start_server(self) -> None:
        ADDON._running = False
        ADDON._online_access_allowed = lambda: False
        with self.assertRaisesRegex(RuntimeError, "[Oo]nline access"):
            ADDON.start_server(19876)
        self.assertFalse(ADDON._running)
        self.assertIsNone(ADDON._server_socket)

        # And it must not have bound a socket that could leak/conflict.
        ADDON._online_access_allowed = lambda: True
        ADDON.start_server(19876)
        try:
            self.assertTrue(ADDON._running)
        finally:
            ADDON.stop_server()

    def test_start_server_blocks_in_background_mode(self) -> None:
        ADDON._running = False
        ADDON._running_in_background = lambda: True
        with self.assertRaisesRegex(RuntimeError, "--background"):
            ADDON.start_server(19876)
        self.assertFalse(ADDON._running)
        self.assertIsNone(ADDON._server_socket)

        # And it must not have bound a socket that could leak/conflict.
        ADDON._running_in_background = lambda: False
        ADDON.start_server(19876)
        try:
            self.assertTrue(ADDON._running)
        finally:
            ADDON.stop_server()

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
        # Each tick processed one command, so it stays at the active rate
        # (0.0 by default) rather than dropping straight to idle.
        self.assertEqual(first_interval, 0.0)
        self.assertEqual(replayed_interval, 0.0)
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
        # Still counts as a tick that did work, even though it was rejected.
        self.assertEqual(interval, 0.0)
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
        self.assertEqual(interval, 0.0)
        self.assertEqual(replayed_interval, 0.0)

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
        self.assertEqual(interval, 0.0)

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

    def test_get_objects_summary_reports_collection_hierarchy(self) -> None:
        child = bpy.data.collections.new("ChildCollection")
        bpy.context.scene.collection.children.link(child)
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.active_object
        for existing_collection in list(obj.users_collection):
            existing_collection.objects.unlink(obj)
        child.objects.link(obj)
        obj.select_set(True)

        summary = ADDON.cmd_get_objects_summary({})
        top = summary["collection"]
        self.assertEqual(top["name"], bpy.context.scene.collection.name)
        nested = next(c for c in top["children"] if c["name"] == "ChildCollection")
        self.assertEqual(len(nested["objects"]), 1)
        obj_summary = nested["objects"][0]
        self.assertEqual(obj_summary["name"], obj.name)
        self.assertEqual(obj_summary["type"], "MESH")
        self.assertIsNone(obj_summary["parent"])
        self.assertEqual(obj_summary["data_name"], obj.data.name)
        self.assertTrue(obj_summary["selected"])
        self.assertTrue(obj_summary["visible"])

    def test_get_window_summary_reports_mode_and_selection(self) -> None:
        obj = bpy.data.objects.new("SelectedObject", None)
        bpy.context.scene.collection.objects.link(obj)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj

        summary = ADDON.cmd_get_window_summary({})
        self.assertEqual(summary["mode"], "OBJECT")
        self.assertEqual(summary["active_object"], "SelectedObject")
        self.assertIn("SelectedObject", summary["selected_objects"])
        self.assertIsInstance(summary["windows"], list)

    def test_jump_to_view3d_object_selects_and_requires_existing_object(self) -> None:
        with self.assertRaises(ValueError):
            ADDON.cmd_jump_to_view3d_object({"name": "DoesNotExist"})

        target = bpy.data.objects.new("JumpTarget", None)
        bpy.context.scene.collection.objects.link(target)
        other = bpy.data.objects.new("OtherObject", None)
        bpy.context.scene.collection.objects.link(other)
        other.select_set(True)

        result = ADDON.cmd_jump_to_view3d_object({"name": "JumpTarget"})
        self.assertEqual(result, {"focused": "JumpTarget"})
        self.assertTrue(target.select_get())
        self.assertFalse(other.select_get())
        self.assertIs(bpy.context.view_layer.objects.active, target)

    def test_jump_to_view3d_object_data_selects_owning_object(self) -> None:
        with self.assertRaises(ValueError):
            ADDON.cmd_jump_to_view3d_object_data({"name": "DoesNotExist"})

        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.context.active_object
        cube.select_set(False)
        bpy.context.view_layer.objects.active = None

        result = ADDON.cmd_jump_to_view3d_object_data({"name": cube.data.name})
        self.assertEqual(result, {"focused": cube.name, "data_name": cube.data.name})
        self.assertTrue(cube.select_get())
        self.assertIs(bpy.context.view_layer.objects.active, cube)

    def test_jump_to_view3d_object_requires_allow_edits_to_unhide(self) -> None:
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.active_object
        obj.hide_set(True)

        with self.assertRaisesRegex(RuntimeError, "allow_edits"):
            ADDON.cmd_jump_to_view3d_object({"name": obj.name})
        self.assertFalse(obj.select_get())

        result = ADDON.cmd_jump_to_view3d_object({"name": obj.name, "allow_edits": True})
        self.assertEqual(result, {"focused": obj.name})
        self.assertFalse(obj.hide_get())
        self.assertTrue(obj.select_get())

    def test_jump_to_view3d_object_requires_allow_edits_to_enable_collection(self) -> None:
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.active_object
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)
        excluded = bpy.data.collections.new("ExcludedCollection")
        bpy.context.scene.collection.children.link(excluded)
        excluded.objects.link(obj)
        layer_collection = next(
            lc for lc in bpy.context.view_layer.layer_collection.children
            if lc.name == excluded.name
        )
        layer_collection.exclude = True

        with self.assertRaisesRegex(RuntimeError, "allow_edits"):
            ADDON.cmd_jump_to_view3d_object({"name": obj.name})

        result = ADDON.cmd_jump_to_view3d_object({"name": obj.name, "allow_edits": True})
        self.assertEqual(result, {"focused": obj.name})
        self.assertFalse(layer_collection.exclude)
        self.assertTrue(obj.select_get())

    def test_jump_to_view3d_object_data_passes_through_allow_edits(self) -> None:
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.active_object
        obj.hide_set(True)

        with self.assertRaisesRegex(RuntimeError, "allow_edits"):
            ADDON.cmd_jump_to_view3d_object_data({"name": obj.data.name})

        result = ADDON.cmd_jump_to_view3d_object_data(
            {"name": obj.data.name, "allow_edits": True}
        )
        self.assertEqual(result, {"focused": obj.name, "data_name": obj.data.name})
        self.assertFalse(obj.hide_get())

    def test_jump_to_tab_by_name_identifies_workspace_and_rejects_unknown(self) -> None:
        # Window.workspace assignment is real but Blender only applies it on
        # its next window-manager event tick (confirmed manually: a deferred
        # bpy.app.timers check sees the switch, an immediate one does not).
        # A single synchronous handler call in this test harness never gets
        # that tick, so only the return value/validation is checked here -
        # the assignment line itself is exercised either way.
        target = next(
            (w for w in bpy.data.workspaces if w.name != bpy.context.window_manager.windows[0].workspace.name),
            None,
        )
        self.assertIsNotNone(target, "test file needs at least two workspaces")

        result = ADDON.cmd_jump_to_tab_by_name({"name": target.name})
        self.assertEqual(result, {"workspace": target.name})

        with self.assertRaises(ValueError):
            ADDON.cmd_jump_to_tab_by_name({"name": "NoSuchWorkspace"})

    def test_jump_to_tab_by_space_type_finds_matching_workspace_and_rejects_unmatched(
        self,
    ) -> None:
        result = ADDON.cmd_jump_to_tab_by_space_type({"space_type": "NODE_EDITOR"})
        matched = bpy.data.workspaces[result["workspace"]]
        self.assertTrue(
            any(area.type == "NODE_EDITOR" for screen in matched.screens for area in screen.areas)
        )

        with self.assertRaises(ValueError):
            ADDON.cmd_jump_to_tab_by_space_type({"space_type": "NOT_A_REAL_SPACE_TYPE"})

    def test_jump_to_tab_by_space_type_creates_workspace_when_allow_edits(self) -> None:
        # No default workspace has a PREFERENCES area.
        with self.assertRaises(ValueError):
            ADDON.cmd_jump_to_tab_by_space_type({"space_type": "PREFERENCES"})

        before_names = {w.name for w in bpy.data.workspaces}
        result = ADDON.cmd_jump_to_tab_by_space_type(
            {"space_type": "PREFERENCES", "allow_edits": True}
        )
        after_names = {w.name for w in bpy.data.workspaces}
        self.assertEqual(len(after_names - before_names), 1)
        new_workspace = bpy.data.workspaces[result["workspace"]]
        self.assertIn(
            "PREFERENCES",
            [area.type for screen in new_workspace.screens for area in screen.areas],
        )

        # A second call must reuse the workspace just created, not duplicate again.
        result2 = ADDON.cmd_jump_to_tab_by_space_type(
            {"space_type": "PREFERENCES", "allow_edits": True}
        )
        self.assertEqual(result2["workspace"], result["workspace"])
        self.assertEqual({w.name for w in bpy.data.workspaces}, after_names)

    def test_get_screenshot_of_area_rejects_unknown_area_type_before_capturing(self) -> None:
        # This path never reaches the real screenshot operator (which needs a
        # display background mode doesn't have), so it's safe to test headless.
        with self.assertRaises(RuntimeError):
            ADDON.cmd_get_screenshot_of_area({"area_ui_type": "NOT_A_REAL_UI_TYPE"})

    def test_get_screenshot_of_area_matches_ui_type_not_broad_area_type(self) -> None:
        # DOPESHEET_EDITOR areas can show either the full Dopesheet
        # (ui_type=DOPESHEET_EDITOR) or the compact Timeline (ui_type=
        # TIMELINE) - both share area.type == 'DOPESHEET_EDITOR', so
        # matching on ui_type is required to tell them apart. Regardless of
        # which sub-mode the default file's dopesheet area is in, at least
        # one of the two must be absent, and asking for it must not fall
        # back to matching the other by its broader area.type.
        area_ui_types_present = {
            area.ui_type
            for window in bpy.context.window_manager.windows
            for area in window.screen.areas
        }
        missing = {"DOPESHEET_EDITOR", "TIMELINE"} - area_ui_types_present
        self.assertTrue(missing, "test file needs a dopesheet-family area for this check")
        with self.assertRaises(RuntimeError):
            ADDON.cmd_get_screenshot_of_area({"area_ui_type": next(iter(missing))})

    def test_downscale_image_to_size_limit_shrinks_a_real_png(self) -> None:
        # A procedurally generated image (not a render or screenshot) is a
        # genuine, varied PNG for the imbuf-based downscale to work on,
        # without needing a GPU/EGL context CI doesn't have - unlike
        # bpy.ops.render.render, generating a COLOR_GRID image is pure CPU
        # work built into Blender core.
        output_path = os.path.join(
            tempfile.gettempdir(), f"mcp_test_downscale_{uuid.uuid4().hex}.png"
        )
        image = bpy.data.images.new("DownscaleTestImage", 400, 400, alpha=False)
        try:
            image.generated_type = 'COLOR_GRID'
            image.filepath_raw = output_path
            image.file_format = 'PNG'
            image.save()
            full_size = os.path.getsize(output_path)

            # A no-op when the file already fits or the limit is disabled (0).
            ADDON._downscale_image_to_size_limit(output_path, 0)
            self.assertEqual(os.path.getsize(output_path), full_size)
            ADDON._downscale_image_to_size_limit(output_path, full_size * 10)
            self.assertEqual(os.path.getsize(output_path), full_size)

            tiny_limit = 5000
            ADDON._downscale_image_to_size_limit(output_path, tiny_limit)
            downscaled_size = os.path.getsize(output_path)
            self.assertLess(downscaled_size, full_size)
        finally:
            bpy.data.images.remove(image)
            try:
                os.remove(output_path)
            except OSError:
                pass

    def test_render_viewport_requires_camera(self) -> None:
        # No active camera, same as the render_scene/render_thumbnail
        # equivalents: this must fail validation before ever reaching
        # bpy.ops.render.render, which needs a GPU/EGL context CI doesn't have
        # (a real render call there aborts the whole process, not just raises
        # a Python exception - so this suite never triggers a real render).
        # bpy.ops.render is a fresh proxy on every access (confirmed: `bpy.ops
        # .render is bpy.ops.render` is False), so it can't be monkeypatched
        # either; the successful-render path is covered by manual testing
        # against a real Blender with GPU/EGL support instead.
        with self.assertRaises(RuntimeError):
            ADDON.cmd_render_viewport({})

    def test_render_thumbnail_limits_are_rejected_before_rendering(self) -> None:
        # No active camera, same as the render_scene equivalent test: invalid
        # size must fail validation before the handler reaches that check.
        for invalid_size in (0, -1, 999999, 512.5, True):
            with self.subTest(size=invalid_size):
                with self.assertRaises((TypeError, ValueError)):
                    ADDON.cmd_render_thumbnail({"size": invalid_size})
        with self.assertRaises(RuntimeError):
            ADDON.cmd_render_thumbnail({"size": 16})

    def test_get_blendfile_summary_datablocks_counts_present_types(self) -> None:
        bpy.data.objects.new("CountedObject", None)
        summary = ADDON.cmd_get_blendfile_summary_datablocks({})
        counts = summary["datablock_counts"]
        self.assertGreaterEqual(counts["objects"], 1)
        self.assertIn("render_engine", summary)
        self.assertIn("active_workspace", summary)

    def test_get_blendfile_summary_missing_files_flags_absent_paths_and_skips_packed(
        self,
    ) -> None:
        missing_image = bpy.data.images.new("MissingImage", 1, 1)
        missing_image.source = "FILE"
        missing_image.filepath = "//does/not/exist.png"

        packed_image = bpy.data.images.new("PackedImage", 1, 1)
        packed_image.pack()

        summary = ADDON.cmd_get_blendfile_summary_missing_files({})
        flagged_names = {entry["name"] for entry in summary["missing"]}
        self.assertIn("MissingImage", flagged_names)
        self.assertNotIn("PackedImage", flagged_names)

    def test_get_blendfile_summary_path_info_reports_unsaved_state(self) -> None:
        real_bpy = ADDON.bpy
        try:
            ADDON.bpy = SimpleNamespace(
                data=SimpleNamespace(filepath="", is_dirty=False),
            )
            info = ADDON.cmd_get_blendfile_summary_path_info({})
        finally:
            ADDON.bpy = real_bpy
        self.assertFalse(info["is_saved"])
        self.assertIsNone(info["filepath"])
        self.assertNotIn("file_size_bytes", info)

    def test_get_blendfile_summary_usage_guess_detects_armature_signal(self) -> None:
        bpy.data.armatures.new("TestArmature")
        summary = ADDON.cmd_get_blendfile_summary_usage_guess({})
        labels = {g["label"] for g in summary["guesses"]}
        self.assertIn("character_rigging", labels)
        # Guesses must be sorted highest-score first.
        scores = [g["score"] for g in summary["guesses"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_get_python_api_docs_resolves_types_properties_and_wildcards(self) -> None:
        type_doc = ADDON.cmd_get_python_api_docs({"identifier": "bpy.types.Object"})
        self.assertEqual(type_doc["identifier"], "bpy.types.Object")
        self.assertTrue(any(p["name"] == "name" for p in type_doc["properties"]))

        prop_doc = ADDON.cmd_get_python_api_docs({"identifier": "bpy.types.Object.location"})
        self.assertEqual(prop_doc["type"], "FLOAT")
        self.assertIn("Location", prop_doc["doc"])

        wildcard = ADDON.cmd_get_python_api_docs({"identifier": "bpy.types.Mesh*"})
        self.assertIn("Mesh", wildcard["matches"])
        self.assertTrue(all(name.startswith("Mesh") for name in wildcard["matches"]))

    def test_get_python_api_docs_rejects_non_bpy_and_unknown_identifiers(self) -> None:
        with self.assertRaises(ValueError):
            ADDON.cmd_get_python_api_docs({"identifier": "os.system"})
        with self.assertRaises(ValueError):
            ADDON.cmd_get_python_api_docs({"identifier": "bpy.types.NotARealType"})

    # PropertyGroup field values live on the Scene datablock itself (that's
    # what lets user settings survive a plain disable/enable in real usage),
    # so they outlive register()/unregister() within one Blender session.
    # Every test below must restore what it changes, rather than assume
    # class defaults, so it doesn't leak state into whichever test runs next.

    def test_register_and_unregister_manage_scene_settings_and_server_lifecycle(
        self,
    ) -> None:
        ADDON._running = False
        ADDON.register()
        try:
            settings = bpy.context.scene.blender_mcp_settings
            self.assertFalse(settings.auto_start)
            self.assertAlmostEqual(settings.poll_interval_active, 0.0, places=5)
            self.assertAlmostEqual(settings.poll_interval_idle, 0.05, places=5)
            self.assertFalse(ADDON._running)
        finally:
            ADDON.unregister()

    def test_auto_start_handler_starts_server_and_is_idempotent(self) -> None:
        ADDON._running = False
        ADDON.register()
        settings = bpy.context.scene.blender_mcp_settings
        try:
            settings.auto_start = True
            settings.port = 19877
            ADDON._auto_start_handler()
            self.assertTrue(ADDON._running)
            self.assertEqual(ADDON._bound_port, 19877)

            # Calling again while already running must not re-bind or error.
            ADDON._auto_start_handler()
            self.assertTrue(ADDON._running)
            self.assertEqual(ADDON._bound_port, 19877)
        finally:
            settings.auto_start = False
            settings.port = 9876
            ADDON.unregister()
        self.assertFalse(ADDON._running)

    def test_auto_start_handler_records_error_instead_of_raising(self) -> None:
        ADDON._running = False
        ADDON.register()
        settings = bpy.context.scene.blender_mcp_settings
        try:
            settings.auto_start = True
            original_socket_factory = ADDON.socket.socket

            def denied_socket(*_args, **_kwargs):
                raise PermissionError("listener creation denied")

            ADDON.socket.socket = denied_socket
            try:
                ADDON._auto_start_handler()  # must not raise
            finally:
                ADDON.socket.socket = original_socket_factory
            self.assertFalse(ADDON._running)
            self.assertIn("Auto-start failed", ADDON._last_server_error or "")
        finally:
            settings.auto_start = False
            ADDON.unregister()

    def test_cli_runner_executes_allowlisted_command(self) -> None:
        bpy.data.armatures.new("CLIArmature")
        response = self.run_cli_command("get_blendfile_summary_usage_guess", {})
        self.assertEqual(response["status"], "ok")
        labels = {g["label"] for g in response["result"]["guesses"]}
        self.assertIn("character_rigging", labels)

    def test_cli_runner_rejects_command_outside_allowlist(self) -> None:
        response = self.run_cli_command("add_primitive", {"type": "cube"})
        self.assertEqual(response["status"], "error")
        self.assertIn("not permitted", response["error"])
        # The handler must never have run.
        self.assertNotIn("Cube", [o.name for o in bpy.data.objects])

    def test_cli_runner_rejects_unknown_command(self) -> None:
        # Rejected by the allowlist before the handler lookup even runs
        # (secure-by-default: unrecognized names never reach dispatch).
        response = self.run_cli_command("this_command_does_not_exist", {})
        self.assertEqual(response["status"], "error")
        self.assertIn("not permitted", response["error"])

    def test_cli_runner_fails_closed_for_allowlisted_but_undispatched_command(self) -> None:
        # CLI_SAFE_COMMANDS is defined as a subset of _HANDLERS, so this
        # shouldn't happen in practice - but the runner must fail closed
        # rather than crash if it ever does.
        original_safe_commands = ADDON.CLI_SAFE_COMMANDS
        ADDON.CLI_SAFE_COMMANDS = original_safe_commands | {"not_a_real_handler"}
        try:
            response = self.run_cli_command("not_a_real_handler", {})
        finally:
            ADDON.CLI_SAFE_COMMANDS = original_safe_commands
        self.assertEqual(response["status"], "error")
        self.assertIn("Unknown command type", response["error"])

    def test_cli_runner_reports_handler_error_without_crashing(self) -> None:
        response = self.run_cli_command("execute_code", {"code": "raise RuntimeError('boom')"})
        self.assertEqual(response["status"], "error")
        self.assertIn("boom", response["error"])

    def test_cli_runner_execute_code_reads_scene_state(self) -> None:
        bpy.data.objects.new("CLICodeObject", None)
        response = self.run_cli_command(
            "execute_code", {"code": "result = [o.name for o in bpy.data.objects]"}
        )
        self.assertEqual(response["status"], "ok")
        self.assertIn("CLICodeObject", response["result"]["result"])

    def test_execute_code_captures_stdout_and_stderr(self) -> None:
        result = ADDON.cmd_execute_code({
            "code": (
                "print('hello stdout')\n"
                "import sys\n"
                "print('hello stderr', file=sys.stderr)\n"
                "result = 1 + 1"
            ),
        })
        self.assertEqual(result["result"], 2)
        self.assertEqual(result["stdout"], "hello stdout\n")
        self.assertEqual(result["stderr"], "hello stderr\n")

    def test_execute_code_omits_stdout_stderr_keys_when_no_output(self) -> None:
        result = ADDON.cmd_execute_code({"code": "result = 42"})
        self.assertEqual(result, {"result": 42})
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)

    def test_execute_code_truncates_oversized_captured_output(self) -> None:
        result = ADDON.cmd_execute_code({
            "code": "print('x' * (ADDON_MAX + 1000))\nresult = None".replace(
                "ADDON_MAX", str(ADDON.MAX_EXECUTE_CODE_OUTPUT_BYTES)
            ),
        })
        self.assertLess(len(result["stdout"].encode("utf-8")), ADDON.MAX_EXECUTE_CODE_OUTPUT_BYTES + 200)
        self.assertIn("truncated", result["stdout"])

    def test_execute_code_blocks_sys_exit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "sys.exit"):
            ADDON.cmd_execute_code({"code": "import sys\nsys.exit(1)"})
        # The guardrail must not leak a patched sys.exit into later code.
        self.assertIs(sys.exit, ADDON.sys.exit)

    def test_execute_code_blocks_listed_destructive_operators(self) -> None:
        for module, func in ADDON._BLOCKED_OPERATORS:
            with self.subTest(op=f"{module}.{func}"):
                with self.assertRaisesRegex(RuntimeError, f"bpy.ops.{module}.{func}"):
                    ADDON.cmd_execute_code({"code": f"bpy.ops.{module}.{func}()"})

    def test_execute_code_allows_mentioning_blocked_names_in_strings_or_comments(
        self,
    ) -> None:
        # The guardrail is an AST check for an actual call expression, not a
        # substring search, so this must not be blocked.
        result = ADDON.cmd_execute_code({
            "code": (
                "# do not call quit_blender here\n"
                "result = 'quit_blender is just a string here'"
            ),
        })
        self.assertEqual(result["result"], "quit_blender is just a string here")

    def test_execute_code_still_allows_ordinary_bpy_ops_calls(self) -> None:
        result = ADDON.cmd_execute_code({
            "code": "bpy.ops.mesh.primitive_cube_add()\nresult = len(bpy.data.objects)",
        })
        self.assertGreater(result["result"], 0)

    def test_execute_code_failure_raises_with_captured_output_attached(self) -> None:
        with self.assertRaises(ADDON._CapturedOutputError) as ctx:
            ADDON.cmd_execute_code({
                "code": "print('before failure')\nraise RuntimeError('boom')",
            })
        self.assertIn("boom", str(ctx.exception))
        self.assertEqual(ctx.exception.stdout, "before failure\n")
        self.assertEqual(ctx.exception.stderr, "")

    def test_execute_code_failure_still_returns_captured_output_via_queue(self) -> None:
        # End-to-end through _execute_queued_command's generic error path,
        # not just cmd_execute_code's own raised exception.
        real_handlers = self._protocol_globals["_HANDLERS"]
        emptied_handlers = ADDON._HANDLERS
        ADDON._HANDLERS = real_handlers
        try:
            command = {
                "id": "print-then-fail",
                "type": "execute_code",
                "params": {"code": "print('before failure')\nraise RuntimeError('boom')"},
            }
            response, _interval = self.process_queued_command(command)
        finally:
            ADDON._HANDLERS = emptied_handlers
        self.assertEqual(response["status"], "error")
        self.assertIn("boom", response["error"])
        self.assertEqual(response["stdout"], "before failure\n")
        self.assertNotIn("stderr", response)

    def test_process_queue_uses_configured_poll_intervals(self) -> None:
        ADDON.register()
        settings = bpy.context.scene.blender_mcp_settings
        try:
            settings.poll_interval_active = 0.01
            settings.poll_interval_idle = 0.25
            self.assertEqual(ADDON._process_queue(), 0.25)
        finally:
            settings.poll_interval_active = 0.0
            settings.poll_interval_idle = 0.05
            ADDON.unregister()

    def test_process_queue_stays_active_for_idle_delay_after_a_burst(self) -> None:
        ADDON.register()
        settings = bpy.context.scene.blender_mcp_settings
        real_handlers = self._protocol_globals["_HANDLERS"]
        emptied_handlers = ADDON._HANDLERS
        try:
            settings.poll_interval_active = 0.01
            settings.poll_interval_idle = 0.25
            settings.poll_interval_idle_delay = 0.03  # ceil(0.03 / 0.01) = 3 ticks
            ADDON._HANDLERS = real_handlers

            # One processed command seeds the idle countdown.
            self.process_queued_command(
                {"id": "seed", "type": "get_scene_info", "params": {}}
            )

            # The next 3 empty-queue ticks must still be at the active rate...
            for tick in range(3):
                with self.subTest(tick=tick):
                    self.assertAlmostEqual(ADDON._process_queue(), 0.01, places=5)
            # ...and only then drop to idle.
            self.assertAlmostEqual(ADDON._process_queue(), 0.25, places=5)
            self.assertAlmostEqual(ADDON._process_queue(), 0.25, places=5)
        finally:
            ADDON._HANDLERS = emptied_handlers
            settings.poll_interval_active = 0.0
            settings.poll_interval_idle = 0.05
            settings.poll_interval_idle_delay = 5.0
            ADDON.unregister()

    def test_use_log_prints_request_and_response_when_enabled(self) -> None:
        ADDON.register()
        settings = bpy.context.scene.blender_mcp_settings
        real_handlers = self._protocol_globals["_HANDLERS"]
        emptied_handlers = ADDON._HANDLERS
        captured = io.StringIO()
        original_stderr = sys.stderr
        try:
            settings.use_log = True
            ADDON._HANDLERS = real_handlers
            sys.stderr = captured
            command = {
                "id": "log-test",
                "type": "get_scene_info",
                "params": {},
            }
            self.process_queued_command(command)
        finally:
            sys.stderr = original_stderr
            ADDON._HANDLERS = emptied_handlers
            settings.use_log = False
            ADDON.unregister()

        output = captured.getvalue()
        self.assertIn("request: get_scene_info", output)
        self.assertIn("response: ok", output)

    def test_use_log_disabled_by_default_prints_nothing(self) -> None:
        real_handlers = self._protocol_globals["_HANDLERS"]
        emptied_handlers = ADDON._HANDLERS
        captured = io.StringIO()
        original_stderr = sys.stderr
        try:
            ADDON._HANDLERS = real_handlers
            sys.stderr = captured
            command = {"id": "no-log-test", "type": "get_scene_info", "params": {}}
            self.process_queued_command(command)
        finally:
            sys.stderr = original_stderr
            ADDON._HANDLERS = emptied_handlers
        self.assertEqual(captured.getvalue(), "")


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
