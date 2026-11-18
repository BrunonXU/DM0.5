"""Turn only after hole departure; preserve tool clearance and plan binding."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation
from f1_harness.contracts import HarnessError
from f1_harness.return_route import propose_return, corners, validate_return_rotation_fraction
from test_return_route import fixture
from test_return_services import return_case


def rotated_fixture():
    cfg=fixture()
    cfg['home'][:3,:3]=Rotation.from_euler('z',90,degrees=True).as_matrix()
    cfg['tool_boxes']=[dict(min=[-.08,-.025,-.025],max=[.08,.025,.025])]
    return cfg


@pytest.mark.parametrize('fraction',[.33,.5])
def test_rotation_follows_departure_with_full_outward_extent(fraction):
    cfg=rotated_fixture();old=propose_return(**cfg,variant=1)
    new=propose_return(**cfg,variant=1,rotation_fraction=fraction)
    assert new['stages'][0]==old['stages'][0]
    assert new['stages'][-1]==old['stages'][-1]
    side=np.array(new['stages'][1]['goals'][0]);old_side=np.array(old['stages'][1]['goals'][0])
    assert not np.allclose(side[:3,:3],old_side[:3,:3])
    right=np.array(cfg['right_axis'])
    tool=corners(cfg['tool_boxes'][0])@side[:3,:3].T+side[:3,3]
    assert min(tool@right)>=new['evidence']['bucket_right_edge']+cfg['clearance_m']-1e-12
    assert side[:3,3]@right>=old_side[:3,3]@right
    assert all(s['eef_object'] is None and s['gripper'] is None for s in new['stages'])


def test_default_route_is_exactly_unchanged():
    cfg=rotated_fixture()
    assert propose_return(**cfg,variant=1)==propose_return(**cfg,variant=1,rotation_fraction=0.)


@pytest.mark.parametrize('bad',[True,False,None,'0.5',-.5,.17,.67,float('nan'),float('inf')])
def test_only_registered_return_rotation_is_accepted(bad):
    with pytest.raises(HarnessError,match='unregistered_return_rotation_fraction'):
        validate_return_rotation_fraction(bad)


def test_coordinated_right_path_still_checks_neighbours():
    cfg=rotated_fixture()
    cfg['obstacles'].append(dict(role='neighbour',min=[-.05,-.4,.51],max=[.05,-.15,.65]))
    with pytest.raises(HarnessError,match='return_route_sweep_blocked:right_of_bucket'):
        propose_return(**cfg,variant=1,rotation_fraction=.5)


def test_revalidation_reconstructs_registered_rotation(return_case):
    serv,clock,obs,intent,observe=return_case
    serv.fixture['return_route']['rotation_fraction']=.5
    serv.fixture['candidate_order']={'withdraw_reset':[1,0,2,3]}
    original_call=serv.numerics.call
    def call(method,**kw):
        result=original_call(method,**kw)
        if method=='forward_pose' and kw['held16'][7]==100:
            result=np.array(result);result[:3,:3]=Rotation.from_euler('z',90,degrees=True).as_matrix()
            return result.tolist()
        return result
    serv.numerics.call=call
    proposal=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        held16=obs.state16,full22=[0]*22))
    after=clock[0];clock[0]+=100_000_000;latest=observe(clock[0],'latest')
    args=dict(intent=intent.model_dump(),proposal=proposal,original_observation=obs.model_dump(),
        observation=latest.model_dump(),full22=[0]*22,held16=obs.state16,after_ns=after)
    serv.dispatch('revalidate_plan',args)
    serv.fixture['return_route']['rotation_fraction']=0.
    with pytest.raises(HarnessError,match='revalidation_stage_binding_mismatch'):
        serv.dispatch('revalidate_plan',args)
