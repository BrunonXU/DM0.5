"""H50 IK adapter for existing 5K/20K/25K services; no model/weight changes.

Original multipart requests go unchanged to the existing GPU endpoint. The
already returned physical H50 output is decoded with the same frozen solver.
"""
import argparse,json,threading,time
from urllib.request import Request,build_opener,ProxyHandler
import numpy as np
from flask import Flask,request,jsonify
from f1_training.execution import frozen_solver
from f1_inference.server import validate_observation

MODE='async_native_h50_v1'
UPSTREAMS={'5k':7892,'20k':7894,'25k':7896}
STEPS={'5k':5000,'20k':20000,'25k':25000}

def expand_response(response, solver):
    result=dict(response,execute_steps=50,execution_mode=MODE)
    if response.get('status')!='ready':return result
    physical=np.asarray(response['diagnostics']['physical_action23'],float)
    if physical.shape!=(50,23) or not np.isfinite(physical).all():raise ValueError('finite physical H50 required')
    anchor=response['anchor_joints14_deg'];reference=response['reference_joints14_deg']
    solved=solver.solve_eef_prefix(physical,anchor,reference,execute_steps=50)
    fields=('joint_targets_deg','gripper_targets','base_velocity_targets')
    for name in fields:result.pop(name,None)
    if not solved.get('success'):
        result.update(status='rejected',reason=solved.get('reason','h50_ik_rejected'),
                      ik={k:solved[k] for k in ('step','arm') if k in solved})
        return result
    for name in fields:
        values=np.asarray(solved[name],float)
        width=14 if name=='joint_targets_deg' else 2 if name=='gripper_targets' else 3
        if values.shape!=(50,width) or not np.isfinite(values).all():raise ValueError('invalid decoded '+name)
        if not np.allclose(values[:20],np.asarray(response[name]),rtol=0,atol=1e-7):
            raise ValueError('H50 decode changed original H20 '+name)
        result[name]=values.tolist()
    return result

def create_app(solver=None, *, response_decoder=expand_response, health_fields=None,
               upstreams=None, checkpoint_steps=None):
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=16777216
    solver=solver or frozen_solver();lock=threading.Lock();opener=build_opener(ProxyHandler({}))
    upstreams=dict(UPSTREAMS if upstreams is None else upstreams)
    checkpoint_steps=dict(STEPS if checkpoint_steps is None else checkpoint_steps)
    if set(upstreams)!=set(checkpoint_steps):raise ValueError('upstream/checkpoint route keys differ')
    def upstream(key,path,body=None,content_type=None):
        if key not in upstreams:raise ValueError('unknown checkpoint route')
        headers={'Content-Type':content_type} if content_type else {}
        with opener.open(Request(f'http://127.0.0.1:{upstreams[key]}{path}',data=body,headers=headers),timeout=10) as r:
            raw=r.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('upstream response too large')
        out=json.loads(raw)
        checkpoint=out.get('model_binding',{}).get('checkpoint_identity',{}).get('checkpoint','')
        if not checkpoint.endswith('/checkpoint-'+str(checkpoint_steps[key])):raise ValueError('checkpoint route binding mismatch')
        return out
    @app.get('/<key>/health')
    def health(key):
        try:
            result=dict(upstream(key,'/health'),execution_mode=MODE,execute_steps=50,
                        adapter='same_physical_h50_same_frozen_ik; no_model_changes')
            result.update(health_fields or {})
            return jsonify(result)
        except Exception as exc:return jsonify(contract='f1_foam_inference_v1',ready=False,error=str(exc)),503
    @app.post('/<key>/process_frame')
    def infer(key):
        if not lock.acquire(False):return jsonify(contract='f1_foam_inference_v1',error='native adapter busy'),409
        try:
            started=time.monotonic();body=request.get_data(cache=True)
            obs=validate_observation(json.loads(request.form['metadata']))
            source=upstream(key,'/process_frame',body,request.content_type)
            for k in ('request_id','observation_monotonic_ns','state19','reference_joints14_deg'):
                if source.get(k)!=obs[k]:raise ValueError('upstream observation mismatch: '+k)
            if source.get('anchor_joints14_deg')!=obs['state19'][:14]:raise ValueError('upstream anchor mismatch')
            decoded=time.monotonic();result=response_decoder(source,solver)
            result['native_adapter_timing_s']={'upstream_http':decoded-started,'h50_ik':time.monotonic()-decoded,'total':time.monotonic()-started}
            return jsonify(result)
        except Exception as exc:
            app.logger.exception('native adapter request failed')
            return jsonify(contract='f1_foam_inference_v1',error=str(exc)),502
        finally:lock.release()
    return app

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=7898);args=p.parse_args()
    create_app().run(host='127.0.0.1',port=args.port,threaded=True,use_reloader=False)
