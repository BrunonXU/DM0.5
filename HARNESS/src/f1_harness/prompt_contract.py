"""F1-specific model contract, generated from the installed robot and helpers.

The pinned upstream member selector is reused; its LIBERO return types and
parameter renderer are not appropriate for F1's dictionary receipts/**kwargs.
This module documents capabilities, never invokes a robot or grants authority.
"""
import ast
import inspect
from pathlib import Path

from pyrualean.prompt import _public_members

from .adapters import F1Robot
from .recovery_programs import compile_library


EXECUTION_CONTRACT = """
You control a persistent Python namespace containing robo and installed helpers.
Deliver a guarded program, not a plan that stops after printing a feasible preview.
Execute all currently decidable steps inside that program. Return for a new model
decision only when new visual interpretation or a new strategy is necessary.
Known bounded recoveries run in the helpers. Never redefine them or wrap unknown
motion in retry loops. You may compose new functions and bounded branches for new
strategies. Unexpected/partial motion and STOP never permit restarting a grasp.
The host does not append actions to a read-only cell. You must explicitly call
the appropriate helper after testing its real prerequisites in the same cell.
Every submitted action still obtains fresh measurements and owner checks.
For a useful local movement, move_and_reobserve(candidates) previews your distinct
move_tcp parameter dictionaries, executes the first accepted motion and obtains
a fresh observation. It never invents a route, bypasses missing scene geometry,
or restarts an unknown action. Replan from its result; keep motion_result if the
subsequent observation fails. This helper does not complete the overall task.
For supported placement, move_tcp(..., support_contact=True) checks a descent
to the registered support with the held object's geometry. It does not open the
gripper or exempt the tool/other obstacles from collision checks. Use current
geometry.support values and choose the pose yourself. release_supported opens
only at a measured contact candidate and verifies a fresh unheld, stationary
object on support. Its success is not insertion/placement and clears old grasp
milestones so a new approach is possible. Missing post-release evidence calls
for recheck of the original request, never another opening command. These APIs
do not provide a prewritten standing-up or regrasp strategy.
Plan around the CURRENT subgoal and its evidence. Future placement uncertainty
does not prove that grasping is impossible. A rejected candidate is not a failed
task. Use diagnostics to change the pose/path or refresh the missing observation.
When a whole route fails, consider a useful intermediate or observation motion
through an available API; preview and owner-check that motion independently,
then reobserve and replan. Never assume unknown space is clear or execute an
unchecked prefix. If the installed API cannot express it, report the capability
gap instead of repeating the same request. Quality scores rank proposals, not
prove collisions or calibrated success probabilities. Preserve actual collision,
limit, feedback and authority checks; never replay unknown publication.
Images are on-demand: movement alone does NOT attach camera images. No initial or failure images are automatically supplied. Explain why numeric
evidence is insufficient using the python tool image_reason argument. show/image/display
requests visual feedback for a later model decision; numeric tool results are
available immediately to the current Python program. Do not request the same view
just to acknowledge it. Variables and functions persist between cells.
RPC ok/completed, a plausible pose, or a preview never proves physical success.
The owner verifies task completion; an already completed task needs no model
confirmation. A partial development goal must not run the full placement macro.
"""

RESULT_CONTRACT = """
F1 results:
- observe(), geometry(), preview(), inspect_plan(), skill(), resume(), recheck()
  and installed helpers return dictionaries: use result['field'] or .get().
- inspect_plan returns an envelope: planned, current_scope, geometry, preview,
  planning_diagnostic/missing_entities when available. preview() returns the
  preview dictionary directly. Inspect both planned and current_scope.
- geometry['grasp_suggestions']['candidates'] entries contain 'parameters'.
  They are suggestions, not IK/success proofs. All are checked before execution.
- Skill receipts: outcome (success/failure/pending_review/interrupted), reason,
  request_id, and optional no_motion, cancel_stage, resume_request_id,
  verification_request_id. Missing fields are unknown; never assume no_motion.
- Complete-program helpers also return program_stage, the last/stopped skill.
  They stop on unresolved failure; do not restart the full macro after movement.
  Keep that receipt. resume_pick(result, hints_by_camera) rechecks its original
  checkpoint and continues remaining stages, including local grasp recovery.
  Supply hints only from the current images when localization needs repair.
- An owner-confirmed no_motion rejection allows a NEW changed plan under current
  authorization. It needs no resume checkpoint. retryable=False does not mean
  alternative strategies are forbidden. Checkpoints are for continuing the SAME
  request. Exhausted registered candidates do not prove physical infeasibility.
- robo.state is F1State (attribute access); tool receipts above are dictionaries.
  Per-cell state is explicitly cached. decision_context derives only from tool
  receipts and is diagnostic, not an execution permit or a new measurement.
Exceptions: validation errors reject arguments; transport/worker exceptions may
leave execution unknown. Inspect original request receipts, never assume that an
exception means nothing moved. EpisodeFinished means the episode has ended.
"""

# Examples use current measurements and helper contracts, never stored poses.
INITIAL_CONTINUATION = '''
# Only before any motion, for the complete placed task.
# If localization is needed, submit ALL visible missing entity hints using the
# supplied views first, then assign initial_plan = robo.inspect_plan().
# If initial_plan was already prepared, reuse it rather than re-fetching it.
g = initial_plan.get('geometry', {})
if initial_plan.get('planned') is True and initial_plan.get('current_scope') is True:
    result = pick_and_place()
else:
    candidates = (g.get('grasp_suggestions') or {}).get('candidates', [])
    if candidates:
        result = pick_with_candidates([c['parameters'] for c in candidates[:6]])
    else:
        result = {'needs_new_strategy': True,
                  'diagnostic': initial_plan.get('planning_diagnostic'),
                  'missing_entities': initial_plan.get('missing_entities')}
print(result)
'''

EXAMPLES = '''
Before any motion, when initial views are supplied but entity hints are missing:
Submit all visible entities grouped by camera through the installed execution
entry localize_and_pick(hints_by_camera). You derive hints from these images;
never copy coordinates from another run. This helper localizes, obtains measured
preview and executes the normal sequence in the SAME cell when feasible.
Example (hints_by_camera contains your actual image-derived dictionaries):
    result = localize_and_pick(hints_by_camera)
    print(result)
Do not stop after a separate inspect_plan merely to ask whether to execute.

Alternative when initial_plan already exists or you explicitly need candidate search:
''' + INITIAL_CONTINUATION + '''
# Alternative at the PRE-MOTION candidate-selection stage:
# candidate_parameters is your list of distinct continuous proposals based on g.
# One call checks candidates and executes the first accepted one through placement.
# It returns after unknown/partial motion; never retry it in an outer loop.
result = pick_with_candidates(candidate_parameters)
print(result)

# Alternative AFTER a skill returned a verification checkpoint:
# For a macro receipt with program_stage, prefer result = resume_pick(result).
# If fresh images require new localization, pass hints_by_camera in that call.
# name/result must be the actual returned skill and receipt, not inferred facts.
# This checks the completed action; it does not replay its movement.
if result.get('outcome') == 'pending_review' and result.get('verification_request_id'):
    result = evidence(name, result)
print(result)
# If still unknown, return its cause/checkpoint. Do not start pick_and_place again.
# If the owner has verified grasp, place_held_block() is the remaining program;
# if approach alone completed, grasp_and_place() is the remaining program.
'''


def api_reference():
    rows = ['F1 robot API (live signatures):', 'robo.CAMERAS = '+repr(F1Robot.CAMERAS)]
    for name, member in _public_members(F1Robot).items():
        fn = member.fget if isinstance(member, property) else member
        signature = inspect.signature(fn)
        signature = signature.replace(parameters=[p for p in signature.parameters.values() if p.name != 'self'])
        head = f'robo.{name}' + (' (property)' if isinstance(member, property) else str(signature))
        rows.append(head + '\n' + (inspect.getdoc(fn) or ''))
    # Read helper signatures without executing their definitions or touching robo.
    tree = ast.parse(compile_library(2))
    public = {'motion','skill','evidence','move_and_reobserve','pick_and_place','pick_with_candidates','localize_and_pick',
              'grasp_and_place','place_held_block','resume_pick'}
    rows.append('Installed persistent helper signatures:')
    rows.extend(f'{node.name}({ast.unparse(node.args)})' for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in public)
    rows.append(RESULT_CONTRACT)
    return '\n\n'.join(rows)


def instructions(local_corrections=2):
    from .astra_programs import recovery_instructions
    knowledge = (Path(__file__).parent/'knowledge/f1.md').read_text()
    return '\n\n'.join((EXECUTION_CONTRACT, api_reference(),
        'Alternative program examples; select the one matching the current stage:\n'+EXAMPLES,
        knowledge, 'Recovery and geometry reference:\n'+recovery_instructions(local_corrections)))


def tool_description(name, original):
    if name != 'python':
        return original
    return ('Execute a Python cell in the persistent F1 workspace. Numeric results are '
            'available immediately for branches and subsequent helper calls in the same cell. '
            'Returns stdout/value, exceptions, a primitive-call ledger and cached state. '
            'Camera images are returned only when explicitly requested with show/image/display; '
            'motion does not automatically attach images. Compose currently decidable steps '
            'and bounded recoveries before yielding for a new model decision.')
