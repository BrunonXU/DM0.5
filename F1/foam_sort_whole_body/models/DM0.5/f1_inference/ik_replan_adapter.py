"""5mm/.05deg demo: return only complete accepted dual-arm timesteps before IK failure."""
import argparse
import numpy as np
from f1_inference.ik_demo_adapter import OPTIONS,RECIPE,UPSTREAMS,STEPS,demo_solver
from f1_inference.ik_demo_adapter import decode_response as decode_demo
from f1_inference.native_adapter import create_app

PREFIX_POLICY = 'f1_ik_valid_prefix_replan_v1'


def decode_response(response, solver):
    result=decode_demo(response,solver)
    result.update(prefix_policy=PREFIX_POLICY,valid_steps=20 if result['status']=='ready' else 0,truncation=None)
    if result['status']=='ready' or result.get('reason')!='ik_rejected':
        return result
    failure=result['ik'];count=failure.get('step')
    if type(count) is not int or not 0<=count<20:
        raise ValueError('IK failure has invalid timestep')
    result['truncation']={'reason':'ik_rejected','step':count,'arm':failure['arm'],
                          'failure':failure.get('failure')}
    if count==0:
        return result
    # The frozen solver retains successful per-arm results in diagnostics even
    # when its public all-or-nothing trajectory is None. Only fully solved pairs
    # before the failed step are eligible; a single arm of that step is excluded.
    diagnostics=result['diagnostics']['ik_details'];joints=[]
    for step in range(count):
        pair=[]
        for arm in (0,1):
            row=diagnostics[step*2+arm]
            if row['step']!=step or row['arm']!=arm or not row['success'] or not row['within_limits']:
                raise ValueError('Incomplete or invalid accepted dual-arm prefix')
            q=np.asarray(row['joints'],float)
            if q.shape!=(7,) or not np.isfinite(q).all():raise ValueError('Invalid accepted joints')
            pair.extend(q.tolist())
        joints.append(pair)
    physical=np.asarray(result['diagnostics']['physical_action23'],float)
    result.update(status='ready',valid_steps=count,joint_targets_deg=joints,
                  gripper_targets=physical[:count,18:20].tolist(),
                  base_velocity_targets=physical[:count,20:23].tolist())
    result.pop('reason',None);result.pop('ik',None)
    return result


def app():
    return create_app(demo_solver(),response_decoder=decode_response,upstreams=UPSTREAMS,checkpoint_steps=STEPS,
                      health_fields={'execution_mode':'sync_raw_h20_v1','execute_steps':20,
                                     'prefix_policy':PREFIX_POLICY,'ik_recipe_version':RECIPE,'ik_options':OPTIONS,
                                     'adapter':'accepted_dual_arm_prefix_then_reobserve; unchanged_model_output'})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=7902)
    args=parser.parse_args();app().run(host='127.0.0.1',port=args.port,threaded=True,use_reloader=False)
