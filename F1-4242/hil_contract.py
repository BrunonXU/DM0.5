"""Portable F1 HIL configuration/HTTP contract; no ROS or output imports.
Canonical copy: DAgger/F1/src/f1dagger/session.py. Workbench copy is verified
byte-for-byte by tests so the two deployable projects remain independent.
"""
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

CONTRACT='f1_upper_16d_v1'
NAMES=[f'arm_{side}_j{i}' for side in ('l','r') for i in range(1,8)]+['left_gripper_position','right_gripper_position']
CAMERAS=['head','left_wrist','right_wrist']
DEFAULT=dict(schema='f1_hil_session_v1',purpose='demonstration',task='F1 upper-body human correction',
             auto_video=False,policy=dict(endpoint='',model_id='',frequency_hz=30.0,chunk_steps=50))

def validate_session(value):
    result=json.loads(json.dumps(DEFAULT));result.update(value)
    if result['schema']!='f1_hil_session_v1' or result['purpose'] not in ('input_check','teleop_play','demonstration','policy_hil'):
        raise ValueError('invalid HIL session purpose/schema')
    if type(result['auto_video']) is not bool or not isinstance(result['task'],str) or len(result['task'])>4000:
        raise ValueError('invalid video/task configuration')
    policy={**DEFAULT['policy'],**value.get('policy',{})};result['policy']=policy
    if not isinstance(policy['endpoint'],str) or len(policy['endpoint'])>2048:raise ValueError('invalid endpoint')
    if policy['endpoint']:endpoint(policy['endpoint'])
    if not isinstance(policy['model_id'],str) or len(policy['model_id'])>256:raise ValueError('invalid model id')
    if type(policy['chunk_steps']) is not int or not 1<=policy['chunk_steps']<=1000:raise ValueError('invalid chunk length')
    if type(policy['frequency_hz']) not in (int,float) or not math.isfinite(policy['frequency_hz']) or policy['frequency_hz']!=30:
        raise ValueError('F1 current action contract requires 30 Hz')
    if result['purpose'] in ('input_check','teleop_play'):result['auto_video']=False
    return {k:result[k] for k in DEFAULT}

def read_session(path):
    raw=Path(path).read_bytes()
    if len(raw)>65536:raise ValueError('session configuration too large')
    return validate_session(json.loads(raw))

def endpoint(value):
    p=urlsplit(value)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('use a credential-free http(s) policy endpoint')
    return value.rstrip('/')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('policy redirects forbidden')

def request_json(url,data=None,content_type='application/json',timeout=2):
    req=Request(url,data=data,headers={'Content-Type':content_type})
    with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=timeout) as response:raw=response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise ValueError('policy response too large')
    result=json.loads(raw)
    if not isinstance(result,dict):raise ValueError('policy object required')
    return result

def capabilities(model_id,version='mock-v1',steps=50):
    return dict(contract=CONTRACT,ready=True,model_id=model_id,model_version=version,
                capabilities=dict(cameras=CAMERAS,state_names=NAMES,action_names=NAMES,
                    units=['degree']*14+['0..100']*2,action_type='absolute',frequency_hz=30.0,chunk_steps=steps))

def validate_health(result,policy):
    if result.get('contract')!=CONTRACT or result.get('ready') is not True:raise ValueError('model not ready / wrong contract')
    if not isinstance(result.get('model_id'),str) or not result['model_id'] or not isinstance(result.get('model_version'),str) or not result['model_version']:
        raise ValueError('missing model identity/version')
    if policy.get('model_id') and policy['model_id']!=result['model_id']:raise ValueError('model identity changed')
    expected=capabilities(result['model_id'],steps=policy['chunk_steps'])['capabilities']
    if result.get('capabilities')!=expected:raise ValueError('model capability/units/order/30Hz/H mismatch')
    return result

def health(policy):return validate_health(request_json(endpoint(policy['endpoint'])+'/health'),policy)

def infer_request(policy,metadata,frames):
    boundary='f1-probe-'+uuid.uuid4().hex;parts=[]
    for name,data,mime in [('metadata',json.dumps(metadata,allow_nan=False).encode(),'application/json')]+frames:
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),data,b'\r\n'])
    parts.append(f'--{boundary}--\r\n'.encode())
    return request_json(endpoint(policy['endpoint'])+'/process_frame',b''.join(parts),'multipart/form-data; boundary='+boundary)

def validate_actions(result,metadata,identity,limits):
    for k in ('contract','request_id','observation_id','policy_epoch','observation_monotonic_ns','frequency_hz'):
        if result.get(k)!=metadata[k] or (k!='frequency_hz' and type(result.get(k)) is not type(metadata[k])):raise ValueError('response identity mismatch: '+k)
    if result.get('model_id')!=identity['model_id'] or result.get('model_version')!=identity['model_version']:raise ValueError('model binding changed')
    actions=result.get('actions')
    if not isinstance(actions,list) or len(actions)!=metadata['chunk_steps']:raise ValueError('wrong H')
    for row in actions:
        if not isinstance(row,list) or len(row)!=16 or any(type(x) not in (int,float) or not math.isfinite(x) for x in row):raise ValueError('finite Hx16 required')
        if any(not limits[n][0]<=v<=limits[n][1] for n,v in zip(NAMES[:14],row[:14])) or any(not 0<=v<=100 for v in row[14:]):raise ValueError('target outside hardware limits')
        if any(1.025*abs(row[o+5])+abs(row[o+6])>110.5+1e-7 for o in (0,7)):raise ValueError('coupled wrist limit')
    return actions
