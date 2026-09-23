"""Two-stage collision-mesh distances, with explicit arm padding.

Coal convex hulls provide the fast, conservative broad phase.  A hull may fill
concave empty space, so every hull result at or below the collision margin is
confirmed against the original triangle meshes before an arm is frozen.
Link7 additionally retains the existing extended proxy as a veto.  No motion
is performed here. Missing geometry/backend failures fail closed.
"""
from __future__ import annotations

import re
import mujoco
import coal
import numpy as np

from _mujoco_types import CollisionDiagnostic
from _mujoco_collision import (_primitive_pair_signed_clearance, _should_skip_primitive_pair,
                               _primitive_pose_world, _primitive_approximate_radius)


class MeshCollisionEvaluator:
    def __init__(self, model, arms, bodies, excluded_pairs, arm_padding):
        self.model = model
        self.padding = float(arm_padding)
        if not np.isfinite(self.padding) or self.padding < 0:
            raise ValueError('invalid arm collision padding')
        excluded = {frozenset(pair) for pair in excluded_pairs}
        geoms = {}
        for primitive in [*arms, *bodies]:
            ids = [i for i in range(model.ngeom)
                   if model.geom_bodyid[i] == primitive.body_id
                   and (model.geom_contype[i] or model.geom_conaffinity[i])]
            # This project's URDF has exactly one collision mesh per link.
            # Refuse silently guessing after an asset/schema change.
            if len(ids) != 1 or model.geom_type[ids[0]] != mujoco.mjtGeom.mjGEOM_MESH:
                raise ValueError(f'expected one collision mesh for {primitive.link_name}, got {ids}')
            geoms[primitive.body_id] = ids[0]
        self.shapes = {}
        self.exact_shapes = {}
        for gid in set(geoms.values()):
            mid = model.geom_dataid[gid]
            start, count = model.mesh_vertadr[mid], model.mesh_vertnum[mid]
            face_start, face_count = model.mesh_faceadr[mid], model.mesh_facenum[mid]
            raw_vertices = np.asarray(model.mesh_vert[start:start+count], dtype=float)
            raw_faces = np.asarray(
                model.mesh_face[face_start:face_start+face_count], dtype=int
            )
            if not len(raw_vertices) or not len(raw_faces):
                raise ValueError(f'collision mesh {gid} has no vertices/faces')
            vertices = coal.StdVec_Vec3s()
            for vertex in raw_vertices:
                vertices.append(np.asarray(vertex, dtype=float))
            self.shapes[gid] = coal.Convex.convexHull(vertices, True, 'Qt')
            exact = coal.BVHModelOBBRSS()
            if exact.beginModel(len(raw_faces), len(raw_vertices)) != 0:
                raise RuntimeError(f'failed to begin triangle mesh {gid}')
            for face in raw_faces:
                if exact.addTriangle(
                    raw_vertices[face[0]], raw_vertices[face[1]], raw_vertices[face[2]]
                ) != 0:
                    raise RuntimeError(f'failed to add triangle to mesh {gid}')
            if exact.endModel() != 0:
                raise RuntimeError(f'failed to finish triangle mesh {gid}')
            self.exact_shapes[gid] = exact
        self.request = coal.DistanceRequest()
        self.request.enable_signed_distance = True
        self.request.gjk_tolerance = 1e-6
        self.request.epa_tolerance = 1e-6
        self.request.gjk_max_iterations = 256
        self.exact_request = coal.DistanceRequest()
        self.exact_request.enable_signed_distance = True
        self.pairs = []
        for arm in arms:
            match = re.fullmatch(r'arm_([LR])_Link([1-7])', arm.link_name)
            if not match:
                raise ValueError(f'unmapped arm collision link: {arm.link_name}')
            index = int(match[2]) - 1 + (7 if match[1] == 'R' else 0)
            for body in bodies:
                if _should_skip_primitive_pair(arm, body, excluded):
                    continue
                self.pairs.append((arm, body, geoms[arm.body_id], geoms[body.body_id], index))
        if not self.pairs:
            raise ValueError('no arm/body collision pairs')
        self._ga = np.array([p[2] for p in self.pairs], dtype=int)
        self._gb = np.array([p[3] for p in self.pairs], dtype=int)
        self._radii = model.geom_rbound[self._ga] + model.geom_rbound[self._gb]

    def evaluate(self, data, margin):
        if not np.isfinite(margin) or not np.all(np.isfinite(data.qpos)):
            raise ValueError('non-finite collision state/margin')
        minimum = float('inf')
        pair = first = second = None
        flags = np.zeros(14, dtype=bool)
        self.last_exact_fallbacks = 0
        transforms = {gid: coal.Transform3s(data.geom_xmat[gid].reshape(3, 3), data.geom_xpos[gid])
                      for gid in self.shapes}
        # Enclosing-sphere lower bounds are conservative. A pair may only be
        # pruned if it cannot alter either the global minimum OR any arm flag.
        bounds = (np.linalg.norm(data.geom_xpos[self._ga]-data.geom_xpos[self._gb], axis=1)
                  - self._radii - self.padding)
        tip_bounds = {}
        for i, (arm, body, _, _, _) in enumerate(self.pairs):
            if arm.link_name.endswith('Link7'):
                ac, _ = _primitive_pose_world(data, arm)
                bc, _ = _primitive_pose_world(data, body)
                lower = (np.linalg.norm(ac-bc) - _primitive_approximate_radius(arm)
                         - _primitive_approximate_radius(body))
                tip_bounds[i] = lower
                bounds[i] = min(bounds[i], lower)
        for i in np.argsort(bounds):
            if bounds[i] > max(minimum, margin):
                continue
            arm, body, ga, gb, index = self.pairs[i]
            result = coal.DistanceResult()
            distance = float(coal.distance(self.shapes[ga], transforms[ga],
                                           self.shapes[gb], transforms[gb], self.request, result))
            if not np.isfinite(distance):
                raise RuntimeError('non-finite mesh collision distance')
            clearance = distance - self.padding
            a, b = result.getNearestPoint1(), result.getNearestPoint2()
            # Convex hulls are conservative but can fill genuine concavities.
            # Only a near/unsafe hull result pays for an exact BVH query; a
            # clearly safe hull also proves the enclosed triangle mesh safe.
            if clearance <= margin:
                self.last_exact_fallbacks += 1
                exact_result = coal.DistanceResult()
                exact_distance = float(coal.distance(
                    self.exact_shapes[ga], transforms[ga],
                    self.exact_shapes[gb], transforms[gb],
                    self.exact_request, exact_result,
                ))
                if not np.isfinite(exact_distance):
                    raise RuntimeError('non-finite exact mesh collision distance')
                clearance = exact_distance - self.padding
                a, b = exact_result.getNearestPoint1(), exact_result.getNearestPoint2()
            if arm.link_name.endswith('Link7'):
                # Preserve the existing 10cm hand extension AND inflated proxy.
                if tip_bounds[i] < clearance:
                    extra, ea, eb = _primitive_pair_signed_clearance(data, arm, body)
                    if not np.isfinite(extra):
                        raise RuntimeError('non-finite extended-tool distance')
                    if extra < clearance:
                        clearance, a, b = extra, ea, eb
            if clearance <= margin:
                flags[index] = True
            if clearance < minimum:
                minimum = clearance
                pair = (f'{arm.link_name}/{arm.collision_name}',
                        f'{body.link_name}/{body.collision_name}')
                first, second = tuple(map(float, a)), tuple(map(float, b))
        return CollisionDiagnostic(safe=minimum > margin, signed_clearance=minimum,
                                   collision_pair=pair, arm_center=first,
                                   body_center=second, arm_collision_flags=flags)
