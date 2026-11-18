"""Robot-side composition; credentials and agent dependencies never enter the owner."""
from pathlib import Path
import json
import math

from .calibration import validate_bundle
from .contracts import Mode, SkillResult
from .engine import TaskEngine
from .evidence import EvidenceStore, AsyncOwnerJournal
from .ipc import UnixClient
from .owner_bridge import OwnerBridge, JointProposal


def build_bridge(config_path: Path, run: Path, *, powered: bool, web_control: bool=False):
    if web_control and not powered:raise ValueError('web_control_requires_hardware_owner')
    config=json.loads(config_path.read_text())
    if config.get("schema")!="f1-harness-owner-config-v1":
        raise ValueError("invalid harness owner configuration")
    development=None
    if config.get('admission_profile') == 'development_coarse':
        from .development import validate_development
        development=validate_development(config_path)
    elif config.get('admission_profile') not in (None,'precision'):
        raise ValueError('unknown_owner_admission_profile')
    if powered and not all(k in config for k in ('stop_delta_deg','stop_window_ms','stop_gripper_delta')):
        raise ValueError('measured_stop_configuration_required')
    stop_fields=('stop_delta_deg','stop_window_ms','stop_gripper_delta')
    numeric=('feedback_age_ms','reference_tolerance_deg')+tuple(
        k for k in stop_fields if powered or config.get(k) is not None)
    if any(not isinstance(config.get(k),(int,float)) or isinstance(config[k],bool)
           or not math.isfinite(config[k]) or config[k]<=0 for k in numeric):
        raise ValueError('positive_measured_owner_configuration_required')
    bundle_path=config.get('calibration_bundle')
    certificate=None
    if bundle_path:
        certificate=validate_bundle(json.loads(Path(bundle_path).read_text()))
        if not certificate['accepted']:raise ValueError('calibration_bundle_not_admitted')
    journal=AsyncOwnerJournal(EvidenceStore(run/'harness'))
    journal.event('run_identity',scenario_id=config.get('scenario_id'),synthetic=False,
                  heldout=config.get('heldout') is True,
                  scene_manifest_sha256=config.get('scene_manifest_sha256'))
    engine=TaskEngine(journal,mode=Mode.HARDWARE if powered else Mode.SHADOW)
    engine.program_trials_enabled=True
    engine.web_control_enabled=bool(web_control)
    if development:
        engine.development_admission=development
        engine.required_task_hash=development['task_hash']
        journal.event('development_admission',**development)
    engine.recording_identity=dict(owner_run_id=run.name,owner_run_path=str(run.resolve()),
        owner_instance_id=engine.instance,host_boot_id=engine.host_boot_id)
    numerical=UnixClient(Path(config['planner_socket']),timeout=92,scene_evidence=True)
    verifier=UnixClient(Path(config['verifier_socket']),timeout=12,scene_evidence=True)
    from .scene_evidence import observation_wire
    def plan(intent,obs,full,held,*,preview=False):
        from .revalidation import require_planning_geometry
        require_planning_geometry(obs,intent=intent,task=engine.task,now_ns=engine.clock(),
                                  max_age_ns=engine.observation_age_ns,
                                  candidate_only=bool(development and development.get('goal')=='placed'))
        value=numerical.call('plan_skill',intent=intent.model_dump(),observation=observation_wire(obs),
                             full22=full,held16=held,preview=preview,grasp_selection=(engine.grasp_selection_for(intent,preview=True)
                                 if preview else engine.grasp_selection_for(intent)))
        return JointProposal(**value)
    def revalidate(intent,proposal,original,latest,full,held,after_ns):
        from dataclasses import asdict
        return numerical.call('revalidate_plan',intent=intent.model_dump(),proposal=asdict(proposal),
            original_observation=observation_wire(original),observation=observation_wire(latest),
            full22=full,held16=held,after_ns=after_ns)
    def verify(intent,after_ns):
        value=verifier.call('verify_skill',intent=intent.model_dump(),after_ns=after_ns)
        result=SkillResult.model_validate(value)
        if result.request_id!=intent.request_id or result.synthetic:
            raise ValueError('verifier_identity_mismatch')
        return result
    def waypoint(intent,proposal,observation,index,full,measured):
        from dataclasses import asdict
        return numerical.call('check_waypoint',intent=intent.model_dump(),proposal=asdict(proposal),
            observation=observation_wire(observation),index=index,full22=full,measured16=measured)
    bridge=OwnerBridge(engine,numerical_planner=plan,result_verifier=verify,
                       feedback_age_ns=int(config['feedback_age_ms']*1e6),
                       reference_tolerance_deg=float(config['reference_tolerance_deg']),
                       candidate_revalidator=revalidate if development and development.get('goal')=='placed' else None,
                       waypoint_verifier=waypoint,
                       stop_delta_deg=.01 if config.get('stop_delta_deg') is None else config['stop_delta_deg'],
                       stop_gripper_delta=config.get('stop_gripper_delta'),
                       stop_window_ns=int((200 if config.get('stop_window_ms') is None else config['stop_window_ms'])*1e6))
    bridge.preview_planner=lambda intent,obs,full,held:plan(intent,obs,full,held,preview=True)
    bridge.geometry_provider=lambda obs,full,held:numerical.call('geometry_context',
        observation=observation_wire(obs),full22=full,held16=held)
    engine.hardware_ready=bool(development or certificate and config.get('integration_validated') is True)
    engine.required_calibration_id=(development['calibration_id'] if development
                                    else certificate['calibration_id'] if certificate else None)
    try:
        bridge.serve(Path(config['owner_socket']))
    except Exception:
        bridge.close()
        journal.close()
        raise
    bridge.journal=journal
    return bridge
