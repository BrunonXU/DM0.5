"""Read-only site configuration audit. This report cannot grant motion authority."""
from pathlib import Path
import json
from .contracts import Task,digest
from .calibration import validate_bundle
from .frames import FrameChain


def inspect_site(directory):
    root=Path(directory).resolve();rows=[];configs={};certificate=None;development=None;coarse_declared=False
    owner_path=root/'owner.local.json'
    if owner_path.is_file():
        try:
            owner=json.loads(owner_path.read_text())
            if isinstance(owner,dict) and owner.get('admission_profile')=='development_coarse':
                coarse_declared=True
                from .development import validate_development
                development=validate_development(owner_path)
                rows.append(dict(component='development_admission',ready=True,reasons=[],receipt=development))
        except (ValueError,KeyError,TypeError,OSError) as exc:
            rows.append(dict(component='development_admission',ready=False,
                             reasons=[type(exc).__name__+':'+str(exc)]))
            if coarse_declared:
                # A rejected coarse contract must not silently become a
                # precision contract and report intentionally absent fields.
                return dict(static_configuration_complete=False,components=rows,
                    admission_profile='development_coarse',live_services_checked=False,
                    physical_admission_checked=False,motion_authorized=False,
                    next_gate='resolve_development_admission_failure')
    def missing(value,prefix=''):
        if value is None:return [prefix]
        if isinstance(value,str) and 'REPLACE_' in value:return [prefix]
        if isinstance(value,dict):return [x for k,v in value.items() for x in missing(v,prefix+'.'+k)]
        if isinstance(value,list):return [x for i,v in enumerate(value) for x in missing(v,prefix+f'[{i}]')]
        return []
    for name in ('owner','task','plan','verify','numerics','gateway'):
        path=root/(name+'.local.json')
        if not path.is_file():rows.append(dict(component=name,ready=False,reasons=['missing_local_configuration']));continue
        try:
            cfg=json.loads(path.read_text())
            if not isinstance(cfg,dict):raise ValueError('configuration_object_required')
            configs[name]=cfg
            # Observation joint mapping may be omitted when scene uses actual owner feedback.
            checks=json.loads(json.dumps(cfg))
            if development:
                # The immutable profile determines which fields the task uses.
                # Full development retains insertion/closure inputs and their sources.
                if name=='owner':checks.pop('calibration_bundle',None)
                if name=='plan':
                    unused=(('insertion_depth_m','effective_clearance_m','grasp_opening',
                             'release_opening','transfer_clearance_m','object_grasp_eef')
                            if development['goal']=='approach_grasp' else
                            ('eef_tip_reference','reference_gripper_closure','maximum_approach_translation_m'))
                    for key in unused:
                        checks.get('fixture',{}).pop(key,None)
                if name=='gateway':
                    if development['goal']=='approach_grasp':
                        for key in ('base_hole','hole_clearance_m','insertion_depth_m','xy_tolerance_m',
                                    'yaw_tolerance_rad','tilt_tolerance_rad','hole_reprojection_px',
                                    'gripper_open_range','uncertainty_m'):
                            checks.get('scene',{}).get('geometry',{}).pop(key,None)
                    else:
                        # Coarse development has no certified absolute error bound.
                        # Collision margin and relative tracking tolerance stay explicit.
                        checks.get('scene',{}).get('geometry',{}).pop('uncertainty_m',None)
                    geometry=checks.get('scene',{}).get('geometry',{})
                    if geometry.get('camera')=='right' and geometry.get('eef_camera') is not None:
                        # validate_development already bound this exact matrix to
                        # the coarse right-hand camera; current FK supplies the base.
                        geometry.pop('base_camera',None)
            if name=='gateway':
                checks.get('observation',{}).pop('joint_state',None)
                geometry=checks.get('scene',{}).get('geometry',{})
                # None disables the optional fixture-occlusion feature. An enabled
                # record must still declare all of its measured inputs.
                occlusion=geometry.get('fixture_occlusion')
                if occlusion is None:
                    geometry.pop('fixture_occlusion',None)
                elif isinstance(occlusion,dict):
                    for key in ('validated','evidence_ids','calibration_id','fixture_id','anchors',
                                'max_occlusion_ns','pixel_tolerance','depth_tolerance_m'):
                        occlusion.setdefault(key,None)
                    if isinstance(occlusion.get('anchors'),dict):
                        for anchor in occlusion['anchors'].values():
                            if isinstance(anchor,dict):
                                anchor.setdefault('roi_center_px',None)
                                anchor.setdefault('depth_m',None)
            reasons=['unfilled:'+p for p in missing(checks)]
            if name=='gateway' and occlusion is not None and not isinstance(occlusion,dict):
                reasons.append('invalid_fixture_occlusion_configuration')
            if name=='task':Task.model_validate(cfg)
            if name=='owner':
                if cfg.get('integration_validated') is not True and not development:reasons.append('integration_not_validated')
                if not all(k in cfg for k in ('stop_delta_deg','stop_window_ms','stop_gripper_delta')):reasons.append('stop_measurement_configuration_missing')
                if cfg.get('calibration_bundle'):
                    bundle_path=Path(cfg['calibration_bundle'])
                    if not bundle_path.is_absolute():bundle_path=root/bundle_path
                    if not bundle_path.is_file():reasons.append('calibration_bundle_unavailable_here')
                    else:
                        certificate=validate_bundle(json.loads(bundle_path.read_text()))
                        reasons.extend(certificate['reasons'])
            if name=='plan':FrameChain.load(cfg['fixture'].get('frame_chain'))
            if name=='gateway':
                FrameChain.load(cfg['scene'].get('frame_chain'))
                if not cfg['observation'].get('source_clock_verified') and not development:reasons.append('source_clock_unverified')
                if not cfg['observation'].get('cameras'):reasons.append('camera_topics_missing')
            rows.append(dict(component=name,ready=not reasons,reasons=reasons,config_sha256=digest(cfg)))
        except (ValueError,KeyError,TypeError,OSError) as exc:
            rows.append(dict(component=name,ready=False,reasons=[type(exc).__name__+':'+str(exc)]))
    if all(k in configs for k in ('task','plan','verify')):
        task=configs['task']
        for name in ('plan','verify'):
            if configs[name].get('task')!=task:rows.append(dict(component=name+'_task_binding',ready=False,reasons=['task_mismatch']))
    if all(k in configs for k in ('plan','gateway')):
        a=configs['plan'].get('fixture',{}).get('frame_chain');b=configs['gateway'].get('scene',{}).get('frame_chain')
        if a!=b:rows.append(dict(component='frame_chain_binding',ready=False,reasons=['scene_plan_frame_chain_mismatch']))
    # Validate links between processes as well as each file's fields. A complete-looking
    # site with a different Unix socket or calibration ID cannot serve a real task.
    def value(component,path):
        result=configs[component]
        for key in path.split('.'):
            if not isinstance(result,dict):return None
            result=result.get(key)
        return result
    def binding(label,references):
        if not all(component in configs for component,_ in references):return
        values=[value(component,path) for component,path in references]
        reason=('binding_value_missing' if any(v is None or v=='' for v in values)
                else 'binding_mismatch' if any(v!=values[0] for v in values[1:]) else None)
        rows.append(dict(component=label,ready=reason is None,reasons=[] if reason is None else [reason],
                         fields=[component+'.'+path for component,path in references]))
    binding('owner_socket_binding',[('owner','owner_socket'),('gateway','owner_socket'),('gateway','scene.owner_socket')])
    binding('plan_socket_binding',[('owner','planner_socket'),('plan','socket')])
    binding('verify_socket_binding',[('owner','verifier_socket'),('verify','socket'),('gateway','scene.verifier_socket')])
    binding('numerical_socket_binding',[('plan','numerical_socket'),('numerics','socket'),('gateway','scene.numerical_socket')])
    binding('scene_calibration_binding',[('task','calibration_id'),('gateway','scene.geometry.calibration_id')])
    binding('scene_object_binding',[('task','object_id'),('gateway','scene.geometry.object_id')])
    binding('scene_hole_binding',[('task','hole_id'),('gateway','scene.geometry.hole_id')])
    binding('native_frame_binding',[('plan','fixture.native_frame'),('plan','fixture.frame_chain.native_frame'),
                                    ('gateway','scene.geometry.native_frame'),('gateway','scene.frame_chain.native_frame')])
    if certificate is not None and 'task' in configs:
        matched=certificate['calibration_id']==configs['task'].get('calibration_id')
        rows.append(dict(component='bundle_calibration_binding',ready=matched,
                         reasons=[] if matched else ['task_calibration_differs_from_bundle']))
    return dict(static_configuration_complete=bool(rows) and all(r['ready'] for r in rows),components=rows,
                admission_profile='development_coarse' if development else 'precision',
                live_services_checked=False,physical_admission_checked=False,motion_authorized=False,
                next_gate=(('full single-block development; genuine local START required'
                            if development['goal']=='placed' else
                            'empty-gripper approach development; genuine local START required') if development
                           else 'live shadow and physical admission; genuine local START required'))
