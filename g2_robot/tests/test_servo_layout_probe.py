import numpy as np
import pytest
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES, POLICY_JOINT_LIMITS
from g2_sort.operations.hardware_tests.servo_layout_probe import read_target, plan_targets, request_groups


def payload_robot():
    values = POLICY_JOINT_LIMITS[:, 0] + np.linspace(.3, .5, 34) * np.ptp(POLICY_JOINT_LIMITS, axis=1)
    class Robot:
        def get_joint_states(self):
            return {'states': [{'name': n, 'motor_position': v} for n, v in zip(POLICY_JOINT_NAMES[:14], values[:14])]}
        def get_end_state(self):
            out = {}
            for side, begin in [('left',14), ('right',24)]:
                indices = list(range(begin,begin+10))[::-1]
                out[side+'_end_state'] = {
                    'names': [POLICY_JOINT_NAMES[i]+(' ' if i==28 else '') for i in indices],
                    'end_states': [{'position': values[i], 'enable':True,'status':0,'err_code':0} for i in indices]}
            return out
    return Robot(), dict(zip(POLICY_JOINT_NAMES,values))


@pytest.mark.parametrize('motion', ['static','left-index','right-index'])
def test_layouts_preserve_every_pair_and_move_only_selected_axis(motion):
    robot, by_name = payload_robot()
    raw,names,b = read_target(robot)
    assert any(n.endswith(' ') for n in raw)
    assert all(n == n.strip() for n in names)
    np.testing.assert_allclose(b, [by_name[n] for n in names])
    t = plan_targets(names,b,motion)
    np.testing.assert_array_equal(t[-1],b)
    if motion == 'static':
        np.testing.assert_array_equal(t,np.tile(b,(100,1)))
    else:
        joint = 'idx37_hand_l_index_pip_joint' if motion=='left-index' else 'idx77_hand_r_index_pip_joint'
        expected = b.copy(); expected[names.index(joint)] += .2
        np.testing.assert_allclose(t[150],expected)
        assert np.count_nonzero(np.max(abs(t-b),axis=0))==1
    for command in t[::20]:
        single=request_groups(names,command,'unified-34')
        dual=request_groups(names,command,'dual-side-17d')
        assert [len(x[0]) for x in single]==[34]
        assert [len(x[0]) for x in dual]==[17,17]
        assert dual[0][0]+dual[1][0]==single[0][0]
        np.testing.assert_array_equal(np.concatenate([x[1] for x in dual]),single[0][1])


def test_reject_zero_hand_unknown_name_and_motion_limit():
    robot,_=payload_robot();_,names,b=read_target(robot)
    zero=b.copy();zero[[i for i,n in enumerate(names) if 'hand_r_' in n]]=0
    with pytest.raises(ValueError,match='all-zero'): plan_targets(names,zero,'static')
    with pytest.raises(ValueError,match='known canonical'):plan_targets(names[:-1]+('bad',),b,'static')
    b[names.index('idx77_hand_r_index_pip_joint')]=1.4
    with pytest.raises(ValueError,match='limits'):plan_targets(names,b,'right-index')
