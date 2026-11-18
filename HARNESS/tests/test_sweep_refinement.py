"""A tighter conservative proof may admit clearance, never sampled contact."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation
from f1_harness.geometry import tool_sweep_clear


def arc_case(radius):
    start=np.eye(4);end=np.eye(4)
    end[:3,:3]=Rotation.from_euler('z',5,degrees=True).as_matrix()
    center=radius*np.array([np.cos(np.deg2rad(2.5)),np.sin(np.deg2rad(2.5)),0])
    obstacle=dict(min=(center-[.00005,.00005,.0005]).tolist(),
                  max=(center+[.00005,.00005,.0005]).tolist())
    options=dict(eef_tip_reference=np.eye(4),radius_m=.1,obstacles=[obstacle],margin_m=0,
        local_boxes=[dict(min=[.9,-.0001,-.001],max=[1.,.0001,.001])])
    return start,end,options


def test_refinement_proves_clearance_without_changing_margin():
    # Every corner rotates within radius sqrt(1 + .0001**2), whereas
    # the obstacle is wholly outside that radius. The old 5-degree chord
    # inflation rejects this gap; smaller conservative intervals prove it.
    start,end,kw=arc_case(1.0003)
    assert tool_sweep_clear(start,end,**kw)
    assert not tool_sweep_clear(start,end,**dict(kw,margin_m=.001))


@pytest.mark.parametrize('radius',[.92,.97,.9999])
def test_midarc_collision_remains_blocked_with_clear_endpoints(radius):
    start,end,kw=arc_case(radius)
    assert tool_sweep_clear(start,start,**kw)
    assert tool_sweep_clear(end,end,**kw)
    assert not tool_sweep_clear(start,end,**kw)


def test_refined_sweep_is_world_frame_covariant():
    start,end,kw=arc_case(1.0003)
    world=np.eye(4);world[:3,:3]=Rotation.from_euler('xyz',[19,34,57],degrees=True).as_matrix()
    world[:3,3]=[.3,-.4,.8]
    kw['obstacles']=[dict(kw['obstacles'][0],pose=world.tolist())]
    assert tool_sweep_clear(world@start,world@end,**kw)


def test_translation_collision_not_lost_by_refinement():
    start,end,kw=arc_case(.97)
    end=start.copy();end[1,3]=.1
    assert not tool_sweep_clear(start,end,**kw)
