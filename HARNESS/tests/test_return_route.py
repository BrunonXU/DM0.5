import numpy as np
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.return_route import propose_return, corners


def fixture():
    current=np.eye(4);current[:3,3]=[0.,0.,.55]
    home=np.eye(4);home[:3,3]=[-.5,-.5,.3]
    hole=np.eye(4);hole[2,3]=.5
    return dict(current=current,home=home,hole=hole,
        obstacles=[dict(role='bucket',min=[-.2,-.2,0.],max=[.2,.2,.5])],
        tool_boxes=[dict(min=[-.025,-.025,-.025],max=[.025,.025,.025])],
        tip=np.eye(4),radius_m=.05,right_axis=[0.,-1.,0.],clearance_m=.02)


def test_right_is_robot_right_and_home_is_only_after_bucket_clearance():
    cfg=fixture();route=propose_return(**cfg)
    assert [s['name'] for s in route['stages']][:2]==['leave_hole','right_of_bucket']
    assert route['stages'][-1]['name']=='ready'
    poses=[np.array(s['goals'][0]) for s in route['stages']]
    assert poses[1][1,3]<-.2-.025-.019
    assert route['evidence']['variant']=='direct_from_right'
    np.testing.assert_array_equal(poses[-1],cfg['home'])


@pytest.mark.parametrize('variant',range(4))
def test_route_follows_moved_bucket_and_checks_neighbour_not_only_destination(variant):
    cfg=fixture();cfg['obstacles'].append(dict(role='neighbour',min=[-.05,-.4,.51],max=[.05,-.15,.65]))
    with pytest.raises(HarnessError,match='return_route_sweep_blocked:right_of_bucket'):
        propose_return(**cfg,variant=variant)
    cfg=fixture();offset=np.array([.2,.3,.1])
    for key in ('current','home','hole'):cfg[key][:3,3]+=offset
    for box in cfg['obstacles']:
        box['min']=(np.array(box['min'])+offset).tolist();box['max']=(np.array(box['max'])+offset).tolist()
    poses=[np.array(s['goals'][0]) for s in propose_return(**cfg,variant=variant)['stages']]
    assert poses[1][1,3]<.3-.2-.025-.019


def test_no_silent_axis_guess_or_omitted_bucket():
    cfg=fixture();cfg['right_axis']=[0,0,1]
    with pytest.raises(HarnessError,match='not_lateral'):propose_return(**cfg)
    cfg=fixture();cfg['obstacles']=[]
    with pytest.raises(HarnessError,match='bucket_geometry'):propose_return(**cfg)


@pytest.mark.parametrize('variant',[1,2,3])
def test_registered_bend_keeps_departure_empty_hand_and_final_ready(variant):
    cfg=fixture();old=propose_return(**cfg);new=propose_return(**cfg,variant=variant)
    assert new['stages'][:2]==old['stages'][:2]
    assert [s['name'] for s in new['stages']]==['leave_hole','right_of_bucket','return_bend','ready']
    np.testing.assert_array_equal(new['stages'][-1]['goals'][0],cfg['home'])
    assert all(s['gripper'] is None and s['eef_object'] is None for s in new['stages'])
    # Block only the new bend. It cannot silently switch to the old route.
    bend=np.array(new['stages'][2]['goals'][0])[:3,3]
    cfg['obstacles'].append(dict(role='neighbour',min=(bend-.005).tolist(),max=(bend+.005).tolist()))
    with pytest.raises(HarnessError,match='return_route_sweep_blocked'):
        propose_return(**cfg,variant=variant)


@pytest.mark.parametrize('variant',[True,-1,4,1.5])
def test_only_registered_return_bends_are_accepted(variant):
    with pytest.raises(HarnessError,match='unregistered_candidate_variant'):
        propose_return(**fixture(),variant=variant)
