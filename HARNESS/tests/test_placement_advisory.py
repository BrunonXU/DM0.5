from dataclasses import replace
from f1_harness.placement_advisory import placement_advisory
from f1_harness.skills import propose,grasp_route_lookahead
from test_full_development_skills import scene


def test_missing_prediction_is_not_a_collision():
    value=placement_advisory([{}])
    assert value['status']=='unavailable'
    assert not value['motion_authorized']


def test_clear_route_remains_predicted_not_numerically_verified_or_measured():
    geometry=replace(scene(),object_held=False,eef_object=None,transfer_policy='registered_transport')
    report=grasp_route_lookahead(geometry,propose('approach_grasp',geometry))
    value=placement_advisory([dict(grasp_route_lookahead=report)])
    assert value['status']=='predicted_clear' and value['clear_transfer_variants']
    assert not value['numerical_feasibility_checked'] and not value['motion_authorized']
    assert value['predicted_attachment_only'] and value['requires_fresh_in_hand_evidence']


def test_fine_geometry_does_not_rewrite_coarse_grasp_or_transport(monkeypatch):
    """Prediction must check each phase against that phase's execution scene."""
    import numpy as np
    from f1_harness.geometry import transform
    import f1_harness.skills as skills
    coarse=replace(scene(),object_held=False,eef_object=None,
                   transfer_policy='registered_transport',aperture_measured=False)
    hole=coarse.base_hole.copy();hole[0,3]+=.002
    precise=replace(coarse,base_hole=hole,aperture_measured=True,
                    obstacles=tuple(dict(o,reference='fine') for o in coarse.obstacles))
    approach=propose('approach_grasp',coarse)
    actual=skills.propose;seen=[]
    def inspect(name,current,**kw):
        seen.append(name)
        if name in ('grasp','transfer_above_hole'):
            assert current.obstacles==coarse.obstacles
            assert not current.aperture_measured
            assert np.array_equal(current.base_hole,coarse.base_hole)
        else:
            assert current.obstacles==precise.obstacles
            assert current.aperture_measured
            assert np.array_equal(current.base_hole,precise.base_hole)
            assert current.object_held is True
            assert np.allclose(current.base_object,transform(current.base_eef)@transform(current.eef_object))
        return actual(name,current,**kw)
    monkeypatch.setattr(skills,'propose',inspect)
    advice=skills.grasp_route_lookahead(coarse,approach,placement_scene=precise)
    assert {'grasp','transfer_above_hole','align','insert','release'} <= set(seen)
    assert advice['precision_placement_preview']['motion_authorized'] is False
