"""Conservative pairing of independently acquired RGB-D frames."""
from __future__ import annotations

from collections import deque
from itertools import product
import threading
import time
import uuid
from pathlib import Path

from .contracts import Frame, Observation, Pairing


class ObservationBuffer:
    def __init__(self, *, source_clock_verified: bool = False, max_skew_ns: int = 20_000_000,
                 capacity: int = 8, common_camera_window_ns: int | None = None, clock=time.monotonic_ns):
        self.instance = uuid.uuid4().hex
        self.host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.clock = clock
        self.source_clock_verified = source_clock_verified
        self.max_skew_ns = max_skew_ns
        self.capacity = capacity
        if common_camera_window_ns is not None and (type(common_camera_window_ns) is not int or common_camera_window_ns <= 0):
            raise ValueError('positive_common_camera_window_required')
        self.common_camera_window_ns=common_camera_window_ns
        self.frames: dict[tuple[str, str], deque] = {}
        self.faults: set[str] = set()
        self.transient_faults: dict[tuple[str, str], str] = {}
        self.rejected_source_frames = 0
        self.duplicate_source_frames = 0
        self._seen: dict[tuple[str, str, int], str] = {}
        self._seq = 0
        self._state = None
        self._mask = None
        self._state_ns = None
        self._lock = threading.RLock()
        self.diagnostics = deque(maxlen=256)
        self.diagnostic_sequence = 0
        self.source_epoch = 0

    def _diagnostic(self, reason, frame=None, previous=None, **details):
        self.diagnostic_sequence += 1
        row = dict(sequence=self.diagnostic_sequence, reason=reason,
                   acquisition_instance=self.instance, source_epoch=self.source_epoch,
                   observed_ns=self.clock(), **details)
        for label, value in (('frame', frame), ('previous', previous)):
            if value is not None: row[label] = value.model_dump()
        self.diagnostics.append(row)

    def diagnostic_events(self, after=0):
        with self._lock:
            return [dict(row) for row in self.diagnostics if row['sequence'] > after]

    def rebuild_source(self, *, reason, evidence):
        """Acquisition-owned epoch change, never an RPC or a planner override.

        A publisher replacement is explicit restart evidence. A source-clock
        reset requires an acquisition adapter's explicit device reset receipt;
        a negative timestamp delta alone is not such a receipt.
        """
        if reason not in ('publisher_changed', 'device_clock_reset') or not evidence:
            raise ValueError('explicit_source_rebuild_evidence_required')
        with self._lock:
            old=self.instance
            self.instance=uuid.uuid4().hex
            self.source_epoch+=1
            self.frames.clear();self._seen.clear();self.transient_faults.clear()
            # Hard resolution/calibration/decoder errors are not cured by a new clock.
            self._diagnostic('acquisition_rebuilt',previous_instance=old,
                             trigger=reason,evidence=evidence)

    def add(self, frame: Frame):
        with self._lock:
            key = (frame.camera, frame.stream)
            frame=frame.model_copy(update={'source_epoch':self.source_epoch})
            queue = self.frames.setdefault(key, deque(maxlen=self.capacity))
            if queue and frame.sequence <= queue[-1].sequence:
                self._diagnostic('sequence_not_increasing',frame,queue[-1],disposition='discard')
                self.transient_faults[key]='sequence_not_increasing:'+frame.camera
                return
            if frame.source_ns is not None:
                stamp = (*key, frame.source_ns)
                previous = self._seen.get(stamp)
                repeated = queue and queue[-1].source_ns == frame.source_ns
                if repeated:
                    self.rejected_source_frames += 1
                    self._diagnostic('duplicate_source_stamp',frame,queue[-1],disposition='discard')
                    if previous and previous != frame.blob_sha256:
                        # Conflicting pixels with one source identity are a real
                        # acquisition fault. Do not silently reuse that identity.
                        self.transient_faults[key] = 'duplicate_source_stamp:' + frame.camera
                    else:
                        # Drivers can redeliver the same source message. Drop
                        # only this delivery: accepted pixels keep their original
                        # timestamp and TTL and cannot become fresh evidence.
                        self.duplicate_source_frames += 1
                    return
                if queue and queue[-1].source_ns is not None and frame.source_ns < queue[-1].source_ns:
                    self._diagnostic('source_time_reversed',frame,queue[-1],disposition='discard',
                                     delta_ns=frame.source_ns-queue[-1].source_ns)
                    self.rejected_source_frames+=1
                    self.transient_faults[key]='source_time_reversed:'+frame.camera
                    return  # Never move the accepted source high-water mark backwards.
                self._seen[stamp] = frame.blob_sha256
                if queue and queue[-1].source_ns is not None and frame.source_ns > queue[-1].source_ns:
                    self.transient_faults.pop(key, None)
            if len(self._seen) > self.capacity * 16:
                self._seen.pop(next(iter(self._seen)))
            queue.append(frame)

    def inspection_frame(self, camera, *, target_ns=None, max_skew_ns=None, max_age_ns):
        """Read an optional RGB frame without changing the mandatory camera snapshot."""
        with self._lock:
            key = (camera, 'rgb')
            frames = self.frames.get(key)
            if not frames or self.faults or self.transient_faults.get(key):
                return None
            selected = (min(frames, key=lambda f: abs(f.received_ns - target_ns))
                        if target_ns is not None else frames[-1])
            if not 0 <= self.clock() - selected.received_ns <= max_age_ns:
                return None
            if (target_ns is not None and max_skew_ns is not None
                    and abs(selected.received_ns - target_ns) > max_skew_ns):
                return None
            return selected

    def add_state(self, state16, mask, received_ns):
        # Validate before publication; no filling missing joints with model placeholders.
        Observation(observation_id="validate", acquisition_instance=self.instance,
                    received_ns=received_ns, clock_domain=self.instance,
                    state16=tuple(state16), state_valid_mask=tuple(mask))
        with self._lock:
            self._state, self._mask, self._state_ns = tuple(state16), tuple(mask), received_ns

    def snapshot(self) -> Observation:
        with self._lock:
            self._seq += 1
            faults = self.faults | set(self.transient_faults.values())
            selected, pairings = [], []
            choices={}
            for camera in sorted({key[0] for key in self.frames}):
                rgbs, depths = self.frames.get((camera, "rgb")), self.frames.get((camera, "depth"))
                if not rgbs:
                    continue
                rgb = rgbs[-1]
                if not depths:
                    selected.append(rgb)
                    continue
                trusted = (self.source_clock_verified and rgb.source_ns is not None
                           and all(f.source_ns is not None and f.clock_domain == rgb.clock_domain for f in depths))
                stamp = (lambda f: f.source_ns) if trusted else (lambda f: f.received_ns)
                # A just-arrived RGB can precede its depth callback. Select the
                # newest complete pair already in the buffer instead of rejecting
                # every such interleaving. Original acquisition times are retained
                # and the downstream age gate still rejects old complete pairs.
                candidates=[]
                for image in reversed(rgbs):
                    if trusted and (image.source_ns is None or image.clock_domain!=rgb.clock_domain):
                        continue
                    match=min(depths,key=lambda f:abs(stamp(f)-stamp(image)))
                    if abs(stamp(match)-stamp(image))<=self.max_skew_ns:
                        candidates.append((image,match))
                choices[camera]=candidates
                if candidates:rgb,depth=candidates[0]
                else:depth=min(depths,key=lambda f:abs(stamp(f)-stamp(rgb)))
                skew = abs(stamp(depth) - stamp(rgb))
                invalid = (skew > self.max_skew_ns or not depth.depth_scale_m
                           or not rgb.camera_info_sha256 or not depth.camera_info_sha256
                           or any(f.endswith(":" + camera) for f in faults))
                quality = "invalid" if invalid else "source_time_bounded" if trusted else "receipt_only"
                selected.append(rgb)
                selected.append(depth)
                pairings.append(Pairing(camera=camera, quality=quality, rgb_sequence=rgb.sequence,
                                        depth_sequence=depth.sequence, skew_ns=skew,
                                        reason="pairing requires verified source clock and calibration"))
            # Simultaneous-camera consumers can explicitly request one window.
            # Fixed-head plus wrist geometry instead matches each camera to its
            # own historical feedback, retaining each newest complete pair.
            if self.common_camera_window_ns is not None and len(choices)>1 and all(choices.values()):
                cameras=tuple(choices)
                best=None;best_score=None
                for combination in product(*(choices[camera] for camera in cameras)):
                    times=[f.received_ns for pair in combination for f in pair]
                    span=max(times)-min(times)
                    if span>self.common_camera_window_ns:continue
                    score=(min(times),-span)
                    if best_score is None or score>best_score:
                        best,best_score=combination,score
                if best is not None:
                    chosen={camera:pair for camera,pair in zip(cameras,best)}
                    selected=[f for camera in cameras for f in chosen[camera]]
                    updated=[]
                    for pair in pairings:
                        rgb,depth=chosen[pair.camera]
                        trusted=(self.source_clock_verified and rgb.source_ns is not None
                                 and depth.source_ns is not None and rgb.clock_domain==depth.clock_domain)
                        skew=abs((rgb.source_ns-depth.source_ns) if trusted
                                 else (rgb.received_ns-depth.received_ns))
                        invalid=(skew>self.max_skew_ns or not depth.depth_scale_m
                            or not rgb.camera_info_sha256 or not depth.camera_info_sha256
                            or any(f.endswith(':'+pair.camera) for f in faults))
                        quality='invalid' if invalid else 'source_time_bounded' if trusted else 'receipt_only'
                        updated.append(pair.model_copy(update=dict(quality=quality,skew_ns=skew,
                            rgb_sequence=rgb.sequence,depth_sequence=depth.sequence)))
                    pairings=updated
            return Observation(observation_id=f"{self.instance}:{self._seq}",
                               acquisition_instance=self.instance, clock_domain=self.instance,
                               host_boot_id=self.host_boot_id,
                               received_ns=self.clock(), frames=tuple(selected), pairings=tuple(pairings),
                               state16=self._state, state_valid_mask=self._mask,
                               state_received_ns=self._state_ns, faults=tuple(sorted(faults)))


COARSE_QUALITY_SCOPES = frozenset(('coarse_static_task', 'coarse_object_verification', 'coarse_fixture_return', 'coarse_local_environment'))


def geometry_age_limit_ns(max_age_ns: int, *, admission_profile='precision', geometry_scope='precision'):
    """Coarse visual age is diagnostic, not a control-feedback watchdog.

    Original acquisition times remain authoritative for identity, ordering and
    post-motion evidence. Live transport/feedback checks own disconnection.
    """
    if admission_profile == 'development_coarse' and geometry_scope in COARSE_QUALITY_SCOPES:
        return None
    return max_age_ns


def stationary_admitted(observation: Observation, *, admission_profile='precision',read_only_reference=False):
    local_evidence=(observation.local_collision_evidence is not None or
        read_only_reference and observation.local_collision_evidence_ref is not None)
    if admission_profile=='development_coarse' and (observation.geometry_scope=='coarse_local_environment'
            or observation.geometry_scope=='coarse_object_verification' and local_evidence):
        # Object-pose stationarity is unknown in this scope. Current physical
        # feedback, owner motion-wait and new occupied/free-space revalidation
        # govern the unloaded observation segment, not a fabricated pose flag.
        return bool(local_evidence
            and observation.state16 is not None and observation.state_valid_mask is not None
            and all(observation.state_valid_mask[7:14]))
    return bool(observation.stationary or (
        admission_profile == 'development_coarse' and observation.geometry_scope in COARSE_QUALITY_SCOPES
        and 'stationary_history_incomplete' in observation.quality_warnings
        and 'observed_motion' not in observation.quality_warnings))


def geometry_quality_warnings(observation: Observation, *, now_ns: int, max_age_ns: int,
                              admission_profile='precision'):
    warnings=list(observation.quality_warnings)
    if geometry_age_limit_ns(max_age_ns, admission_profile=admission_profile,
                             geometry_scope=observation.geometry_scope) is None:
        times=(observation.received_ns, observation.state_received_ns,
               *(f.received_ns for f in observation.frames))
        if any(stamp is not None and now_ns-stamp>max_age_ns for stamp in times):
            warnings.append('visual_measurements_above_age_budget')
    return tuple(dict.fromkeys(warnings))


def require_geometry_identity(observation: Observation, *, required_cameras=("head", "right"),
                              goal="placed", admission_profile="precision", skill=None, verification=False,
                              read_only_reference=False,hole_id=None):
    reference=observation.local_collision_evidence_ref if read_only_reference else None
    local_evidence=observation.local_collision_evidence is not None or reference is not None
    if reference is not None and (reference.observation_id!=observation.observation_id
            or reference.calibration_id!=observation.calibration_id):
        raise ValueError('scene_evidence_observation_binding_mismatch')
    if skill=='withdraw_reset' and observation.geometry_scope!='coarse_fixture_return':
        raise ValueError('fixture_return_scope_required')
    local=(admission_profile=='development_coarse' and goal=='placed'
           and (observation.geometry_scope=='coarse_local_environment'
                or observation.geometry_scope=='coarse_object_verification'
                   and local_evidence and skill in ('move_tcp','reobserve')))
    if local:
        if skill not in ('reobserve','move_tcp') or not local_evidence:
            raise ValueError('local_environment_only_supports_world_observation_motion')
        required_cameras=(tuple(v.camera for v in observation.local_collision_evidence.free_space_views)
            if observation.local_collision_evidence is not None else reference.cameras)
    object_only=(admission_profile=='development_coarse' and goal=='placed'
                 and observation.geometry_scope=='coarse_object_verification'
                 and (skill in ('verify_grasp','inspect_in_hand','reobserve')
                      or verification and skill in ('grasp','regrasp','recover_grasp','release_supported',
                                                     'transfer_above_hole')))
    """Check geometry structure independently; this alone never admits motion."""
    development = goal == "approach_grasp" and observation.geometry_scope == "coarse_static_approach"
    full_development=(admission_profile=='development_coarse' and goal=='placed'
                      and observation.geometry_scope=='coarse_static_task')
    if (hole_id and admission_profile=='development_coarse' and goal=='placed'
            and observation.geometry_scope in ('coarse_static_task','coarse_object_verification')
            and skill in ('align','insert','release','verify_placed')
            and not any(o.object_id==hole_id and o.pose is not None and o.evidence_ids
                        for o in observation.objects)):
        from .geometry_context import aperture_missing_reason
        raise ValueError('aperture_measurement_required:'+hole_id+':'+aperture_missing_reason(observation))
    retained_fixture=(full_development and observation.fixture is not None
                      and observation.fixture.source=='retained_static_fixture')
    if retained_fixture:
        from .geometry_context import FIXTURE_TRANSIT_SKILLS
        if skill not in FIXTURE_TRANSIT_SKILLS|{'verify_grasp','inspect_in_hand','reobserve'}:
            # Static obstacle memory is not a current aperture measurement.
            raise ValueError('full_scene_required_for_motion')
    return_only=(admission_profile=='development_coarse' and goal=='placed'
                 and observation.geometry_scope=='coarse_fixture_return' and skill=='withdraw_reset')
    development=development or full_development or object_only or return_only or local
    if observation.geometry_scope != "precision" and not development:
        raise ValueError("coarse_geometry_cannot_authorize_precision_task")
    if observation.synthetic:
        raise ValueError("synthetic observations are not hardware geometry")
    if (observation.faults or not stationary_admitted(observation, admission_profile=admission_profile,
                                                    read_only_reference=read_only_reference)
            or not observation.calibration_id):
        raise ValueError("stationary calibrated observation required")
    if observation.state_received_ns is None:
        raise ValueError("fresh physical state required")
    if observation.state16 is None or observation.state_valid_mask is None or not all(observation.state_valid_mask[7:14]):
        raise ValueError("valid measured right-arm state required")
    if development and not (local and skill=='reobserve') and not observation.state_valid_mask[15]:
        raise ValueError('fresh measured gripper required for coarse tip')
    if development and not local and (not full_development or retained_fixture):
        if observation.geometry_camera not in ('head','right'):
            raise ValueError('explicit_development_camera_required')
        required_cameras = (observation.geometry_camera,)
    for camera in required_cameras:
        pairs = [p for p in observation.pairings if p.camera == camera]
        permitted = ("source_metadata_matched", "source_time_bounded", "receipt_only") if development else (
            "source_metadata_matched", "source_time_bounded")
        if not pairs or pairs[-1].quality not in permitted:
            raise ValueError("unverified RGB-D pairing: " + camera)
    return required_cameras


def require_geometry(observation: Observation, *, now_ns: int, max_age_ns: int,
                     required_cameras=("head", "right"), goal="placed", admission_profile="precision",
                     skill=None, verification=False,read_only_reference=False,hole_id=None):
    required_cameras=require_geometry_identity(observation,required_cameras=required_cameras,
        goal=goal,admission_profile=admission_profile,skill=skill,verification=verification,
        read_only_reference=read_only_reference,hole_id=hole_id)
    age_limit=geometry_age_limit_ns(max_age_ns,admission_profile=admission_profile,
                                    geometry_scope=observation.geometry_scope)
    def current(stamp):
        return stamp is not None and 0<=now_ns-stamp and (age_limit is None or now_ns-stamp<=age_limit)
    if not current(observation.received_ns):
        raise ValueError("stale observation")
    if not current(observation.state_received_ns):
        raise ValueError("fresh physical state required")
    for camera in required_cameras:
        for frame in (f for f in observation.frames if f.camera == camera):
            if not current(frame.received_ns):
                raise ValueError("stale frame: " + camera)
