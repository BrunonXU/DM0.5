"""Select the reviewed B1250 profile inside a frozen task configuration."""
from copy import deepcopy


def build_profile(catalog, scene, *, seed_review=None):
    """Derive model conditioning/transport from the registered B1250 recipe."""
    model = catalog['models']['v1_dagger_stage_b_1250']
    contract = catalog['contracts'][model['contract']]
    policy = deepcopy(contract['config']['policy'])
    policy.update(endpoint=f"http://127.0.0.1:{model['local_port']}",
                  model_id=model['model_id'], rtc_pixels_directory=model['pixels'])
    geometry = scene['geometry']
    result = dict(schema='f1-vla-astra-profile-v1', policy=policy, rlt_enabled=False,
        hybrid=dict(enabled=True, base_prompt=contract['prompt'],
            base_model_version=model['model_version'], frame_chain=deepcopy(scene['frame_chain']),
            # B1250 returns to fully open (0); local Harness releases at 18.
            # Both are open. Reusing the narrow 18±0.5 fixture band would
            # incorrectly call B's open hand closed and accept stale advice.
            native_frame=geometry['native_frame'], gripper_open_range=[0,geometry['gripper_open_range'][1]],
            # A broad consultation window, never a motion admission bound.
            region=dict(hole_pose=[x for row in geometry['base_hole'] for x in row],
                        radius_m=.20, height_m=.35, bottom_m=-.05),
            return_position_m=.025, return_angle_rad=.15))
    if seed_review is not None:
        from .target_relocalization import source_search_box, rectified_search_box
        from .hybrid import checked_review
        decision = checked_review(seed_review['decision'])
        result['search_priors'] = {}
        for camera in ('head','right'):
            view = geometry if camera==geometry['camera'] else geometry['hole_camera']
            meta = seed_review['metadata'][camera]
            boxes = {e['object_id']:rectified_search_box(source_search_box(meta,e['box_normalized']),view)['box']
                     for e in decision[camera+'_entities']}
            result['search_priors'][camera] = dict(boxes=boxes,
                image_sha256=meta['image_sha256'],camera_info_sha256=view['rgb_info_sha256'],
                source='Astra medium automatic localization; search only')
    return result


def configure_hybrid(documents, template, *, remote_site):
    controller = documents['controller.local.json']
    candidate = deepcopy(controller.get('hybrid_candidate'))
    if not isinstance(candidate, dict) or candidate.get('schema') != 'f1-vla-astra-profile-v1':
        raise ValueError('reviewed_hybrid_profile_not_installed')
    policy = candidate['policy']
    if (policy.get('model_id') != 'f1-toys-eef-81444e7071723c1f'
            or policy.get('transport') != 'eef_websocket_v2'
            or candidate.get('rlt_enabled') is not False):
        raise ValueError('hybrid_requires_frozen_B1250_without_RLT')
    controller['policy'] = policy
    controller['hybrid'] = candidate['hybrid']
    controller['hybrid']['enabled'] = True
    controller['hybrid']['frame_chain']['development_owner_config'] = remote_site+'/owner.local.json'
    documents['gateway.local.json']['scene']['geometry']['gripper_open_range'] = candidate['hybrid']['gripper_open_range']
    controller.pop('rlt_policy', None)
    controller.pop('evaluation', None)
    # These registered windows are automatic segmentation search seeds only.
    # They do not assert object identity, attachment or a motion-ready pose.
    for camera, view in documents['masks.local.json']['views'].items():
        view['require_explicit_target_prompt'] = False
        prior = candidate.get('search_priors', {}).get(camera)
        if prior:
            geometry=documents['gateway.local.json']['scene']['geometry']
            calibrated=geometry if camera==geometry['camera'] else geometry['hole_camera']
            if prior['camera_info_sha256']!=calibrated['rgb_info_sha256']:
                raise ValueError('hybrid_search_prior_camera_changed')
            view['prompts'].update(prior['boxes'])
