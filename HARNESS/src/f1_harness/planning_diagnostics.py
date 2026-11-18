"""Bound numerical explanations without dropping the identity of a collision."""
from copy import deepcopy
import json

PRIORITY=('skill','stage_index','route_family','search_phase','frame','position_error_reference',
          'position_error_m','rotation_error_rad','position_tolerance_m','rotation_tolerance_rad',
          'best_fit_executable','motion_authorized','path_checked','grasp_checked','search_exhaustive',
          'ik_seed_count','ik_pose_fit_count','native_endpoint_checks','native_valid_goal_count','ompl_started',
          'best_fit_native_checked','best_fit_native_valid','endpoint_rejection','recovery_hint',
          'target_tcp_pose','best_fit_tcp_pose','target_minus_fit_tcp_m',
          'collision_location','obstacle_role','obstacle_name',
          'cell_min','cell_max','possible_target_overlap','views','cells_tested',
          'refinement_budget_exhausted','evidence_observation_id','unknown_space_is_free','clear',
          'collision_body','start_pose_clear','end_pose_clear','endpoint_clearance_hint','segment_index','goal_index','variant',
          'segment_start_tcp_pose','segment_end_tcp_pose','missing_entities',
          'continuation_skill','continuation_code','termination','cartesian_stalled_ticks','position_error_m','rotation_error_rad',
          'grasp_strategy','grasp_pitch_offset_deg','recovery_hint',
          'min_constraint_margin_rad','active_planes','collision_pair','clearance_m')


def bound_detail(value,limit=4095):
    result=deepcopy(value)
    size=lambda:len(json.dumps(result,ensure_ascii=False))
    if size()<=limit:return result
    result['detail_truncated']=True
    last=result.get('last_candidate_failure')
    if isinstance(last,dict):
        # Preserve the final implementation's reason even after earlier
        # candidate details are shortened. This duplicate has a small fixed
        # diagnostic scope; full geometry belongs in the original receipt.
        last_keys=('skill','stage_index','route_family','continuation_skill','continuation_code',
                   'obstacle_role','obstacle_name','collision_body','collision_location',
                   'position_error_m','rotation_error_rad','clearance_m','termination',
                   'search_phase','position_error_reference','native_endpoint_checks','native_valid_goal_count',
                   'ompl_started','best_fit_executable','motion_authorized','path_checked','search_exhaustive')
        last_keys+=('cell_min','cell_max','possible_target_overlap','views','cells_tested',
                    'refinement_budget_exhausted','evidence_observation_id','unknown_space_is_free','clear')
        last['metrics']={k:v for k,v in last.get('metrics',{}).items()
                         if k in last_keys}
    bulky=('segment_start_tcp_pose','segment_end_tcp_pose','goal','final_arm_deg','limits',
           'before','after','active_planes','target_tcp_pose','best_fit_tcp_pose','target_minus_fit_tcp_m',
           'best_fit_eef_pose')
    # Keep the top-level rejected segment's poses; repeated candidate poses
    # are less useful than which obstacle and whether its endpoint is blocked.
    for candidate in result.get('candidates',[]):
        metrics=candidate.get('metrics',{})
        for key in bulky:metrics.pop(key,None)
    if size()<=limit:return result
    for entry in [result,*result.get('candidates',[])]:
        metrics=entry.get('metrics',{})
        for key in list(metrics):
            if key not in PRIORITY:metrics.pop(key)
    while size()>limit and result.get('candidates'):
        result.setdefault('candidate_count',len(result['candidates']))
        # Top-level last failure remains complete; first candidates retain
        # their own reason for as long as the byte budget permits.
        result['candidates'].pop()
    if size()>limit:
        metrics=result.get('metrics',{});result['metrics']={}
        for key in PRIORITY:
            if key not in metrics:continue
            result['metrics'][key]=metrics[key]
            if size()>limit:result['metrics'].pop(key)
    return result
