"""Compact pregrasp lookahead for selection, never future motion authority."""


def with_transport(result,stages):
    native=next((s['native_grasp_lookahead']['transport'] for s in stages
        if isinstance(s.get('native_grasp_lookahead'),dict)
        and isinstance(s['native_grasp_lookahead'].get('transport'),dict)),None)
    if native is None:return result
    checked=native.get('checked_variants',[])
    result['geometry_status']=result['status']
    result['transport_numerics']=dict(status=native['status'],
        source=native['source'],predicted_attachment_only=True,motion_authorized=False,
        actual_transfer_requires_new_measurements=True,feasible_variant=native.get('feasible_variant'),
        checked_variant_count=len(checked),reason=native.get('reason'))
    failures=[]
    for row in checked:
        if row.get('planned') is True:continue
        detail=row.get('diagnostic',{});metrics=detail.get('metrics',{})
        failures.append(dict(variant=row['variant'],code=detail.get('code'),metrics={k:metrics[k]
            for k in ('position_error_m','rotation_error_rad','goal_index','route_family',
                      'collision_body','obstacle_name','collision_location') if k in metrics}))
    if failures:result['transport_numerics']['failures']=failures
    if native['status']=='no_registered_route':
        # A registered route from the predicted endpoint can fail even when
        # a later measured observation/reorientation enables transport.
        # Do not turn this conditional search result into global infeasibility.
        result['status']='direct_transfer_unresolved'
        result['blocked_skill']='transfer_above_hole'
        result['transport_numerics']['scope']='registered_routes_from_predicted_grasp_endpoint'
        result['transport_numerics']['recovery_checked']=False
    elif native['status']!='predicted_feasible':
        result['status']='unavailable'
    # The old flag concerns the entire placement, including alignment/insertion.
    # A feasible transfer does not silently certify those later native paths.
    return result


def placement_advisory(stages):
    lookahead=next((stage['grasp_route_lookahead'] for stage in stages
                    if isinstance(stage.get('grasp_route_lookahead'),dict)),None)
    result=dict(status='unavailable',predicted_attachment_only=True,
                numerical_feasibility_checked=False,motion_authorized=False)
    if lookahead is None:return with_transport(result,stages)
    transport=lookahead.get('transport_preview',{})
    if transport.get('status')=='unresolved':
        result.update(status='direct_transfer_unresolved',blocked_skill='transfer_above_hole',
                      transport_geometry=transport)
        return with_transport(result,stages)
    preview=lookahead.get('precision_placement_preview',{})
    candidates=preview.get('candidates',[])
    if not candidates:return with_transport(result,stages)
    clear=[c.get('transfer_variant') for c in candidates
           if c.get('outcome')=='geometrically_clear']
    result.update(status='predicted_clear' if clear else 'predicted_blocked',
                  candidate_count=len(candidates),clear_transfer_variants=clear,
                  requires_fresh_in_hand_evidence=True)
    # One detailed obstruction is sufficient to explain why another grasp is
    # useful. Keep all original per-route diagnostics in the owner plan trace.
    blocked=next((c for c in candidates if c.get('outcome')=='advisory'),None)
    if blocked is not None:
        attempts=blocked.get('alignment_attempts',[])
        detail=(attempts[-1].get('diagnostic',{}) if attempts else
                blocked.get('diagnostic',{}))
        fields=('skill','route_family','collision_location','obstacle_role',
                'obstacle_name','collision_body','start_pose_clear','end_pose_clear')
        result['obstruction']=dict(blocked_skill=blocked.get('blocked_skill'),
            code=detail.get('code'),metrics={key:detail['metrics'][key] for key in fields
                if key in detail.get('metrics',{})})
    return with_transport(result,stages)
