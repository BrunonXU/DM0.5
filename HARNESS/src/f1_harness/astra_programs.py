"""Install the baseline's registered recovery helpers in PyRUA's persistent cells."""
import hashlib

from .contracts import HarnessError
from .recovery_programs import PROGRAMS, compile_library, registered_program, validate_program


def install_recovery_helpers(arm, local_corrections, store, *, regrasp_limit=0, grasp_observation_enabled=False):
    """Define helpers without calling a skill; owner checkpoints remain authoritative.

    Use the public Python cell tool rather than modifying the pinned upstream
    interpreter or exposing an additional robot capability. The cell and this
    receipt bind the exact compiler output used by this run.
    """
    source = compile_library(local_corrections, regrasp_limit, grasp_observation_enabled=grasp_observation_enabled)
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    marker = 'f1-recovery-library-ready:' + source_sha256
    result = arm.execute_tool('python', {'code': source + '\n' + repr(marker)}).result
    if (result.get('error') or result.get('exception') or result.get('episode_finished')
            or result.get('value') != repr(marker) or result.get('moved')
            or any(item.get('tool') == 'execute_skill' for item in result.get('primitive_calls', []))):
        store.event('planner_recovery_helpers_failed', source_sha256=source_sha256,
                    local_corrections=local_corrections, result=result)
        raise HarnessError('planner_recovery_helpers_not_installed')
    receipt = dict(source_sha256=source_sha256, local_corrections=local_corrections,regrasp_limit=regrasp_limit,
                   grasp_observation_enabled=grasp_observation_enabled,
                   program_ids={name: validate_program(registered_program(name)) for name in PROGRAMS},
                   helper_names=['motion', 'skill', 'evidence', 'move_and_reobserve', 'pick_and_place', 'pick_with_candidates',
                                 'grasp_and_place', 'place_held_block', 'continue_external_task', 'localize_and_pick', 'resume_pick'], cell=result.get('cell'),
                   motion_requested=False)
    store.event('planner_recovery_helpers_ready', **receipt)
    return receipt


def recovery_instructions(local_corrections):
    """Workflow only; the generated robot API reference owns parameter details."""
    compile_library(local_corrections)
    return f'''
Use installed helpers for known continuations; do not redefine them or retry
unknown motion. You may write new functions and bounded branches composing
public skills for a new strategy. Helpers do not exhaust the action space.
The persistent Python workspace already contains initial_plan (default preview)
and g (its measured geometry) when initial planning context was available.
Read those variables directly: do not spend a model turn re-fetching or printing
the same geometry. If geometry is absent or the scene changed, inspect_plan can
refresh both. inspect_plan returns an envelope: p=initial_plan['preview']; direct
robo.preview returns p itself. pa=p.get('placement_advisory', {{}});
tn=pa.get('transport_numerics', {{}}). Wrist visibility is in each p['stages']
entry's wrist_view.inside_image, not a top-level wrist_view/status field.
inside_image=False is a projected visibility warning, not collision or a veto.
Do not add a `visible` condition before pick_and_place; the helper still requires
actual grasp evidence and uses the authorized observation recovery if needed.
For a rejected default, first read g.get('grasp_suggestions') or {{}}. Its candidates
contain parameters derived locally from the measured object axes and size, ranked
by geometric route prediction and travel cost. Try these parameter dictionaries
before generating more numeric guesses. They are NOT native-IK checked, contact
proof or an exhaustive search. An empty/blocked sample permits a new strategy.
Use result = pick_with_candidates([c['parameters'] for c in g['grasp_suggestions']['candidates']]).
It previews at most six distinct candidates, executes a feasible approach and
continues the full guarded program in ONE cell. If submission rejects a changed
scene with owner-confirmed zero publication and registered_candidates_exhausted,
it tries the next candidate. Unknown/partial motion, STOP, scope changes and failed
continuations return immediately; the helper never starts another grasp after
approach has moved. candidate_search records attempted indices and request IDs;
full diagnostics already appear in the tool ledger. Do not wrap it in retry loops.
If advice is absent/exhausted, supply a new list of up to six continuous proposals.
Prefer candidates with placement_advisory.status=predicted_clear and
transport_numerics.status=predicted_feasible. predicted_blocked means change grasp
offset/orientation using its diagnostic. direct_transfer_unresolved means only
registered transport routes failed from the predicted grasp endpoint; recovery
was NOT checked. Prefer another grasp with feasible transport, or a changed path.
This is pa['status']; tn['status'] instead uses predicted_feasible,
no_registered_route, incomplete or unavailable. It is not global unreachability
or permission to fabricate recovery success.
unavailable means unknown, not a veto. All advice uses predicted attachment;
actual transport needs measured holding, and alignment/insertion remain unchecked.
Native checks and skill admission still decide each request; preview is not
motion authority. Return the helper result without retrying completed/unknown
motion. If exhausted, change strategy using its ledger on the next model turn.
termination=cartesian_no_progress means this numerical search stopped improving,
not global unreachability; choose a distinct grasp/path instead of repeating it.
Print only outcome/stage/diagnostics, not another full geometry dump. Search seeds
are derived from current geometry; they are not a fixed allowed-pose catalogue.
Geometry is included in the structured primitive_calls ledger for failed
inspect_plan and explicit geometry reads; it must not be recovered from stdout.
Repeated obstacle transforms use pose_ref/obstacle_pose_frames in that summary;
the Python variable g retains its original obstacle poses. g['current_tcp_pose_object']
gives the current TCP in object coordinates. Consider its orientation alongside
g['nominal_grasp_tcp_pose'], not as the only orientation family. For the square,
sample distinct face-aligned yaw symmetries in object coordinates and change tilt
and contact offset using object_local_box. Current wrist yaw need not align to a
graspable face; repeatedly perturbing that yaw can miss every usable family.
Diversify approach sides and grasp offsets when rejected, not just small angle
changes around one failed family. All candidates still require native previews.
For endpoint_clearance_hint, translation_m is in the object's axes: it separates
the tool endpoint from ONE obstacle at unchanged orientation. Use it to reason
about a changed grasp or pregrasp; it does not prove jaw contact, IK, other
obstacles, or a clear path. Never execute it as an escape from a colliding start.
For sweep_envelope with both endpoints clear, change the connecting path instead
of moving the grasp endpoint blindly. No clearance hint authorizes a motion.
Normal task: result = pick_and_place(**approach_parameters). It performs approach,
grasp, verify_grasp, deterministic transport, in-hand inspection at the bucket,
align, insert, release, verify_placed and withdraw_reset in one Python cell.
If approach already succeeded, call grasp_and_place(). If grasp is already
verified, call place_held_block(). Never repeat completed motion. Set withdraw=False
only when withdraw_reset is absent from allowed_skills. Stop the sequence on any
result whose outcome is not success; program_stage identifies the stopped step.
On an unsubmitted align blocked by observation, this program refreshes the
current aperture locally. If needed it previews one bound, previously clear
held-arm viewpoint, moves there, and remeasures before resuming align. Read
placement_handoff for the original request, measured holding, missing entity,
numeric left-wrist observations and any viewpoint already tried. Choose a
different observation strategy if still unresolved. After obtaining a measured
aperture, resume_pick(result) rejoins at align without repeating transport.
An owner-verified grasp miss can use the prepared regrasp budget inside this helper;
missing visual evidence cannot trigger regrasp. When recover_grasp is authorized,
the helper can perform one closed-gripper observation motion after unknown grasp
verification. It then verifies actual attachment; unknown results return without
transfer. Do not repeat observation motions or reinterpret them as grasp success.
A failed regrasp returns without transfer.

motion(name, **parameters) handles owner-issued resume and evidence checkpoints;
skill(name, **parameters) handles evidence only; evidence(name, result) continues
an already returned verification without moving again. Helpers allow at most {local_corrections} local corrections;
the owner enforces the task budget. A resumable result has outcome="failure",
reason="motion_stalled", cancel_stage="measured_stationary" and resume_request_id.
If a pre-grasp resume has only local collision evidence, the helper can preview
one newly planned move to the wrist pose where this same target was last
measured, then reobserve and resume the saved checkpoint. The original
no_motion=False still describes the published approach. continuation_submitted=False
describes only the blocked continuation. Read observation_recovery before choosing
a different view or supplying new image hints to resume_pick; never restart pickup.
A read-only continuation has outcome="pending_review" and verification_request_id.
Unknown/partial publication and interruption must not be replayed. A checkpoint
is required to RESUME the same request, not to propose a NEW plan after an
owner-confirmed zero-publication rejection. retryable=False describes replay of
that request; it does not declare all alternative strategies impossible.

For no_motion=True and numerical_plan_rejected:registered_candidates_exhausted,
use planning_diagnostic to change the pose/path, then execute the changed candidate;
do not repeat an identical rejected plan. Missing checkpoints are expected when
nothing was published. Read current geometry and propose a different plan.
After a changed move_tcp succeeds while holding is verified, the remaining
placement suffix is place_held_block(start='inspect_in_hand'); calling the
default place_held_block() would retry the original transport. The changed move
is not proof of arrival above the bucket: check its measured endpoint before
choosing the suffix. Use object-relative grasp_tcp_pose or TCP
waypoints from robo.inspect_plan(); registered variants are shortcuts, not
an exhaustive action space. Preview is not execution or success.

Perception: numeric evidence first. If images have been explicitly requested and
supplied, use their exact source frames. Otherwise never invent visual hints. search_entities gives registered IDs, NOT detections.
On initial missing geometry, submit visible block, fixture and aperture prompts
in ONE cell using locate_entities once per camera, THEN call inspect_plan once.
Do not call observe between these prompts: an unprompted view cannot recover by
waiting. Never invent a hint for an invisible entity; report its identity.
For later failures, recover only the named missing entities; a missing aperture
is not a missing block. A measured fixture may suffice for approach/grasp/transport;
use the actual skill preview rather than missing_entities alone. Alignment,
insertion and release need measured aperture geometry. Boxes/points use the
DISPLAYED image coordinates, including its crop, normalized to [0,1]. Optional
positive/negative points separate holes from the lid; exclude gripper/shadow.
The owner bounds physical action corrections by {local_corrections}. Zero-motion
planning searches are not physical retries. Candidate exhaustion is not proof of
infeasibility: change the route or observation strategy within the task budget.
Do not repeat unchanged failures; finish stuck when no justified next strategy remains. Closed fingers alone are not proof of holding an object.

Example of guarding the next skill:
result = pick_and_place()
'''
