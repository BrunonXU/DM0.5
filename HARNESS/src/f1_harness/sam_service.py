"""Persistent SAM2 GPU inference. Pixels and prompts only; no robot authority."""
import argparse
import hashlib
import io
import json
import os
import socket
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2
import numpy as np


class RemoteSam2Segmenter:
    def __init__(self,url,*,model_sha256,timeout=2.):
        import httpx
        if urlparse(url).hostname not in ('127.0.0.1','localhost'):
            raise ValueError('sam_requires_loopback_tunnel')
        self.url=url.rstrip('/');self.model_sha256=model_sha256
        self.client=httpx.Client(timeout=timeout,trust_env=False)

    def segment(self,rgb,*,box,points=None,labels=None):
        return self._request(rgb,box=box,points=points,labels=labels)[0]

    def segment_many(self,rgb,*,prompts):
        """One image upload and encoder state for multiple independently named masks."""
        requests=[]
        for key,args in prompts.items():
            item=dict(object_id=key,box=np.asarray(args['box']).tolist())
            if args.get('points') is not None:
                item['point_prompt']=dict(points=np.asarray(args['points']).tolist(),
                                          labels=np.asarray(args['labels']).tolist())
            requests.append(item)
        validate_batch_prompts(requests,rgb.shape[:2])
        results,_=self._request(rgb,batch=requests)
        return dict(zip(prompts,results))

    def track(self,rgb,*,box,stream_id,object_id,source_ns,reseed=False,points=None,labels=None,discover=False):
        results,metadata=self._request(rgb,box=box,points=points,labels=labels,tracking=dict(stream_id=stream_id,
            object_id=object_id,source_ns=source_ns,reseed=reseed,discover=discover))
        if metadata.get('score_kind')!='object_presence_not_mask_iou':
            raise ValueError('sam_tracking_contract_changed')
        return results[0],metadata

    def discover(self,rgb,*,stream_id,object_id,source_ns):
        """Use current full-image semantics; a stored pixel box is no identity."""
        result,metadata=self.track(rgb,box=[0,0,rgb.shape[1],rgb.shape[0]],
            stream_id=stream_id,object_id=object_id,source_ns=source_ns,discover=True)
        if metadata.get('seed_scope')!='full_image_semantics':
            raise ValueError('sam_discovery_contract_changed')
        return result,metadata

    def _request(self,rgb,*,box=None,tracking=None,points=None,labels=None,batch=None):
        rgb=np.asarray(rgb,np.uint8)
        ok,encoded=cv2.imencode('.png',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_PNG_COMPRESSION,1])
        if not ok:raise ValueError('sam_image_encode_failed')
        payload=encoded.tobytes();identity=hashlib.sha256(payload).hexdigest()
        headers={'X-SAM-Image':identity,'X-SAM-Model':self.model_sha256}
        if batch is None:headers['X-SAM-Box']=json.dumps(np.asarray(box,float).tolist())
        else:headers['X-SAM-Prompts']=json.dumps(batch)
        if tracking is not None:headers['X-SAM-Tracking']=json.dumps(tracking)
        if points is not None:
            headers['X-SAM-Points']=json.dumps(dict(points=np.asarray(points).tolist(),labels=np.asarray(labels).tolist()))
        response=self.client.post(self.url+'/predict',content=payload,headers=headers)
        if response.status_code!=200:
            raise ValueError('sam_inference_failed:'+str(response.status_code))
        if (response.headers.get('X-SAM-Image')!=identity or
                response.headers.get('X-SAM-Model')!=self.model_sha256 or len(response.content)>8*1024**2):
            raise ValueError('sam_response_identity_changed')
        with np.load(io.BytesIO(response.content),allow_pickle=False) as data:
            masks=data['masks'];scores=data['scores']
        if masks.shape!=(len(scores),*rgb.shape[:2]) or not np.isfinite(scores).all():
            raise ValueError('sam_response_shape_changed')
        if batch is not None:
            expected=[item['object_id'] for item in batch]
            if len(scores)!=len(expected) or json.loads(response.headers.get('X-SAM-Entities','null'))!=expected:
                raise ValueError('sam_response_entities_changed')
        metadata=json.loads(response.headers.get('X-SAM-Tracking-Result','{}'))
        if tracking is not None and any(metadata.get(k)!=tracking[k] for k in ('stream_id','object_id','source_ns')):
            raise ValueError('sam_tracking_response_identity_changed')
        return [(mask.astype(bool),float(score)) for mask,score in zip(masks,scores)],metadata

    def close(self):self.client.close()


def validate_batch_prompts(requests,shape):
    """Validate the complete request before a predictor consumes any prompt."""
    from .perception import validate_point_prompt
    if not isinstance(requests,list) or not 1<=len(requests)<=16:
        raise ValueError('sam_batch_invalid')
    result={};height,width=shape[:2]
    for item in requests:
        if not isinstance(item,dict) or set(item)-{'object_id','box','point_prompt'}:
            raise ValueError('sam_batch_invalid')
        key=item.get('object_id')
        if not isinstance(key,str) or not 0<len(key)<=128 or key in result:
            raise ValueError('sam_batch_identity_invalid')
        box=np.asarray(item.get('box'),float)
        if (box.shape!=(4,) or not np.isfinite(box).all() or
                not 0<=box[0]<box[2]<=width or not 0<=box[1]<box[3]<=height):
            raise ValueError('sam_box_invalid')
        args=dict(box=box)
        if 'point_prompt' in item:
            points,labels=validate_point_prompt(item['point_prompt'],shape)
            args.update(points=points,labels=labels)
        result[key]=args
    return result


def track_frame(tracker,segmenter,seed_provider,rgb,*,box,stream_id,object_id,
                source_ns,reseed=False,discover=False,point_args=None):
    """Associate a seed, then use the upstream video predictor unchanged.

    A semantic miss preserves existing video memory but yields no measurement.
    Later current-image semantics can reidentify the object. This is deliberately
    not an alternative tracker or a mask-quality threshold.
    """
    point_args=point_args or {};key=(stream_id,object_id)
    previous=tracker.tracks.get(key)
    pending=bool(previous and previous.get('identity_pending'))
    needs_seed=reseed or previous is None or pending
    scope='full_image_semantics' if discover else 'current_hint'
    timing=dict(seed_inference_ms=0.,seed_inference_count=0,tracking_inference_ms=0.,tracking_inference_count=0)

    def propose_seed():
        result=None;detail=dict(seed_backend='sam2',seed_reason='recipe_sam2',
            identity_status='confirmed',identity_source='explicit_prompt')
        configured=seed_provider is not None and object_id in seed_provider.prompts
        if discover and not configured:
            return None,dict(seed_backend=None,seed_reason='semantic_entity_unconfigured',
                identity_status='unresolved',identity_source='semantic')
        if seed_provider is not None:
            result,detail=seed_provider.seed(rgb,object_id=object_id,box=box,**point_args)
        if result is None and configured and not point_args:
            # In particular, do not turn a fully occluded block into a SAM2
            # mask of the gripper merely because it is inside the search box.
            return None,dict(detail,identity_status='unresolved',identity_source='semantic')
        if result is None:result=segmenter.segment(rgb,box=box,**point_args)[0]
        return result,dict(detail,identity_status='confirmed',
            identity_source='semantic' if detail.get('seed_backend')=='sam3' else 'explicit_prompt')

    def seed():
        start=time.perf_counter()
        try:return propose_seed()
        finally:
            timing['seed_inference_ms']+=(time.perf_counter()-start)*1000
            timing['seed_inference_count']+=1

    def propagate(seed_mask,reset):
        start=time.perf_counter()
        try:return tracker.track(rgb,stream_id=stream_id,object_id=object_id,
            source_ns=source_ns,seed_mask=seed_mask,reset=reset)
        finally:
            timing['tracking_inference_ms']+=(time.perf_counter()-start)*1000
            timing['tracking_inference_count']+=1

    def missing(detail,tracked=None):
        current=tracker.tracks.get(key)
        if current is not None:current['identity_pending']=True
        metadata=dict(stream_id=stream_id,object_id=object_id,source_ns=source_ns,
            score_kind='object_presence_not_mask_iou',motion_evidence=False,
            reseeded=False,seed_attempted=True,seed_scope=scope,
            tracking_memory_preserved=current is not None,**detail)
        metadata.update(identity_status='unresolved',identity_reason=detail.get('seed_reason'))
        if tracked is not None:
            metadata.update({k:v for k,v in tracked.items() if k not in ('mask','object_presence_score')})
        metadata.update(timing)
        return (np.zeros(rgb.shape[:2],bool),0.),metadata

    seed_mask=None;quality=None;detail={}
    if needs_seed:
        proposal,detail=seed()
        if proposal is None:return missing(detail)
        seed_mask,quality=proposal
        if not np.asarray(seed_mask).any():
            return missing(dict(detail,seed_reason='sam_tracking_seed_empty'))
    tracked=propagate(seed_mask,reseed or pending)
    if not tracked['mask'].any() and not needs_seed:
        proposal,detail=seed()
        if proposal is None:return missing(detail,tracked)
        seed_mask,quality=proposal
        if not np.asarray(seed_mask).any():
            return missing(dict(detail,seed_reason='sam_tracking_seed_empty'),tracked)
        tracked=propagate(seed_mask,True)
        needs_seed=True
    metadata={k:v for k,v in tracked.items() if k!='mask'}
    metadata.update(reseeded=needs_seed,seed_attempted=needs_seed,
        seed_mask_quality=quality,seed_scope=scope,**detail)
    metadata.update(identity_status='confirmed' if tracked['mask'].any() else 'unresolved',
        identity_source=detail.get('identity_source','track'),
        identity_reason=detail.get('seed_reason','same_video_identity'))
    metadata.update(timing)
    return (tracked['mask'],tracked['object_presence_score']),metadata


def serve(config,*,segmenter=None,tracker=None,seed_provider=None):
    checkpoint=Path(config['checkpoint']);identity=hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    if identity!=config['checkpoint_sha256']:raise ValueError('sam_checkpoint_identity_changed')
    if segmenter is None:
        import torch
        from .perception import Sam2Segmenter
        torch.set_num_threads(2)
        if not config['device'].startswith('cuda') or not torch.cuda.is_available():
            raise ValueError('sam_gpu_required_no_cpu_fallback')
        segmenter=Sam2Segmenter(config['model_config'],checkpoint,device=config['device'])
    if config.get('enable_streaming',False) and tracker is None:
        from .sam_tracking import Sam2StreamingTracker
        tracker=Sam2StreamingTracker(config['model_config'],checkpoint,device=config['device'])
    if config.get('seed_provider') is not None and seed_provider is None:
        from .sam_seed import Sam3SeedProvider
        seed_provider=Sam3SeedProvider(config['seed_provider'],config['device'])
    # Warm before advertising readiness; never add model loading to each task.
    started=time.perf_counter()
    segmenter.segment(np.zeros((480,640,3),np.uint8),box=[200,100,400,300])
    if tracker is not None:
        # Warm video propagation too; model startup must not land on START.
        warm_mask=np.zeros((480,640),bool);warm_mask[100:300,200:400]=True
        tracker.track(np.zeros((480,640,3),np.uint8),stream_id='warmup',object_id='warmup',
            source_ns=0,seed_mask=warm_mask)
        tracker.reset()
    if seed_provider is not None:
        for object_id in seed_provider.prompts:
            seed_provider.seed(np.zeros((480,640,3),np.uint8),object_id=object_id,box=[200,100,400,300])
    warmup_s=time.perf_counter()-started
    lock=threading.Lock();stats=dict(requests=0,last_inference_ms=None)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def setup(self):
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path!='/health':self.send_error(404);return
            data=json.dumps(dict(ready=True,backend='sam2',model_sha256=identity,
                device=config['device'],pid=os.getpid(),warmup_s=warmup_s,streaming=tracker is not None,
                recipe=config.get('recipe',{'status':'legacy','tracking':'sam2'}),
                seed_model_sha256=getattr(seed_provider,'identity',None),**stats)).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            try:
                self.connection.settimeout(3)
                length=int(self.headers.get('Content-Length','0'))
                if self.path!='/predict' or not 0<length<=8*1024**2:raise ValueError('sam_request_size')
                if self.headers.get('X-SAM-Model')!=identity:raise ValueError('sam_model_request_identity')
                payload=self.rfile.read(length);image_id=hashlib.sha256(payload).hexdigest()
                if image_id!=self.headers.get('X-SAM-Image'):raise ValueError('sam_image_request_identity')
                bgr=cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_COLOR)
                if bgr is None or bgr.shape[0]*bgr.shape[1]>1920*1080:raise ValueError('sam_image_shape')
                rgb=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB)
                batch_header=self.headers.get('X-SAM-Prompts');batch=None
                if batch_header is not None:
                    if len(batch_header)>32768 or any(self.headers.get(key) is not None for key in
                            ('X-SAM-Box','X-SAM-Points','X-SAM-Tracking')):
                        raise ValueError('sam_batch_invalid')
                    batch=validate_batch_prompts(json.loads(batch_header),rgb.shape[:2])
                box=np.asarray(json.loads(self.headers.get('X-SAM-Box','null')),float)
                if batch is None and (box.shape!=(4,) or not np.isfinite(box).all() or
                        not 0<=box[0]<box[2]<=rgb.shape[1] or not 0<=box[1]<box[3]<=rgb.shape[0]):
                    raise ValueError('sam_box_invalid')
                point_args={}
                point_header=self.headers.get('X-SAM-Points')
                if point_header is not None:
                    if len(point_header)>4096:raise ValueError('sam_points_invalid')
                    from .perception import validate_point_prompt
                    points,labels=validate_point_prompt(json.loads(point_header),rgb.shape[:2])
                    point_args=dict(points=points,labels=labels)
                with lock:
                    before=time.perf_counter();metadata={}
                    tracking_header=self.headers.get('X-SAM-Tracking')
                    if batch is not None:
                        result=[segmenter.segment(rgb,**arguments)[0] for arguments in batch.values()]
                    elif tracking_header is None:
                        result=segmenter.segment(rgb,box=box,**point_args)
                    else:
                        if tracker is None or len(tracking_header)>2048:raise ValueError('sam_tracking_unavailable')
                        request=json.loads(tracking_header)
                        stream_id=request['stream_id'];object_id=request['object_id'];source_ns=request['source_ns']
                        if (not isinstance(stream_id,str) or not 0<len(stream_id)<=256 or
                                not isinstance(object_id,str) or not 0<len(object_id)<=128 or
                                type(source_ns) is not int or source_ns<0 or
                                type(request.get('reseed',False)) is not bool or
                                type(request.get('discover',False)) is not bool):
                            raise ValueError('sam_tracking_identity_invalid')
                        discover=request.get('discover',False)
                        if discover:
                            if point_args:raise ValueError('sam_discovery_requires_semantics')
                            box=np.asarray([0,0,rgb.shape[1],rgb.shape[0]],float)
                        item,metadata=track_frame(tracker,segmenter,seed_provider,rgb,box=box,
                            stream_id=stream_id,object_id=object_id,source_ns=source_ns,
                            reseed=request.get('reseed',False),discover=discover,point_args=point_args)
                        result=[item]
                    stats.update(requests=stats['requests']+1,last_inference_ms=(time.perf_counter()-before)*1000)
                stream=io.BytesIO();np.savez_compressed(stream,masks=np.stack([r[0] for r in result]),scores=[r[1] for r in result])
                data=stream.getvalue();self.send_response(200)
                self.send_header('Content-Length',str(len(data)));self.send_header('X-SAM-Image',image_id)
                self.send_header('X-SAM-Model',identity)
                if batch is not None:self.send_header('X-SAM-Entities',json.dumps(list(batch)))
                if metadata:self.send_header('X-SAM-Tracking-Result',json.dumps(metadata))
                self.end_headers();self.wfile.write(data)
            except (ValueError,KeyError,TypeError):self.send_error(400,'invalid SAM request')
            except (RuntimeError,OSError):self.send_error(503,'SAM inference unavailable')
    server=ThreadingHTTPServer(('127.0.0.1',config['port']),Handler)
    server.daemon_threads=True
    print(json.dumps(dict(ready=True,model_sha256=identity,device=config['device'],port=config['port'])),flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True)
    serve(json.loads(parser.parse_args().config.read_text()))
