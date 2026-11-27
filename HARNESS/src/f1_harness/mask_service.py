"""Separate bounded image channel for a pinned SAM2 worker; no actuator or planner API."""
import io
import json
import re
import threading
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import urlparse
from itertools import combinations

import numpy as np


class MaskClient:
    def __init__(self,url,*,timeout=2,view=None):
        import httpx
        if urlparse(url).hostname not in ('127.0.0.1','localhost'):raise ValueError('mask_service_requires_loopback_tunnel')
        self.client=httpx.Client(timeout=timeout,trust_env=False)
        self.url=url.rstrip('/')+'/segment'
        self.view=view

    def __call__(self,rgb,view=None):
        selected=self.view if view is None else view
        if selected is not None and (not isinstance(selected,str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}',selected)):
            raise ValueError('invalid_mask_view')
        stream=io.BytesIO();np.save(stream,rgb,allow_pickle=False)
        headers={} if selected is None else {'X-Harness-View':selected}
        response=self.client.post(self.url,content=stream.getvalue(),headers=headers)
        if response.status_code!=200:
            # Only the bounded diagnostic fields from our loopback worker are
            # propagated; do not substitute a generic error for its actual gate.
            try:detail=response.json() if len(response.content)<=4096 else {}
            except ValueError:detail={}
            reason=detail.get('reason','unknown') if isinstance(detail,dict) else 'unknown'
            object_id=detail.get('object_id') if isinstance(detail,dict) else None
            raise ValueError('mask_worker_rejected_frame:'+json.dumps(dict(
                status=response.status_code,view=selected,object_id=object_id,
                reason=str(reason)[:160]),sort_keys=True))
        if selected is not None and response.headers.get('X-Harness-View')!=selected:
            raise ValueError('mask_view_response_mismatch')
        if len(response.content)>8*1024*1024:raise ValueError('mask_response_too_large')
        with np.load(io.BytesIO(response.content),allow_pickle=False) as values:
            masks={key:values[key].astype(bool) for key in values.files}
        if any(mask.shape!=rgb.shape[:2] for mask in masks.values()):raise ValueError('mask_dimensions_changed')
        return masks

    def close(self):self.client.close()


def _view_state(config,segmenter):
    from uuid import uuid4
    backend=config.get('backend','sam2')
    if backend not in ('sam2','fixed_color','sam2_remote'):raise ValueError('unknown_mask_backend')
    if segmenter is None:
        if backend=='fixed_color':
            from .perception import FixedColorSegmenter
            segmenter=FixedColorSegmenter(config['color_profiles'])
        elif backend=='sam2_remote':
            from .sam_service import RemoteSam2Segmenter
            segmenter=RemoteSam2Segmenter(config['sam_url'],model_sha256=config['checkpoint_sha256'],
                timeout=config.get('timeout_seconds',2.))
        else:
            from .perception import Sam2Segmenter
            from hashlib import sha256
            from pathlib import Path
            if sha256(Path(config['checkpoint']).read_bytes()).hexdigest()!=config['checkpoint_sha256']:
                raise ValueError('segmentation_checkpoint_identity')
            segmenter=Sam2Segmenter(config['model_config'],config['checkpoint'],device=config['device'])
    prompts=dict(config['prompts'])
    optional=set(config.get('optional_ids',[]))
    forwarded={frozenset(pair) for pair in config.get('scene_validated_overlap_pairs',[])}
    context_regions=set(config.get('context_region_ids',[]))
    if not context_regions<=set(prompts):raise ValueError('context_mask_identity_mismatch')
    if not optional.issubset(prompts) or any(len(pair)!=2 or not pair.issubset(prompts) for pair in forwarded):
        raise ValueError('invalid_mask_identity_configuration')
    tracking=config.get('fixed_color_tracking_padding_px')
    group=config.get('fixture_tracking_group')
    if group is not None and (backend not in ('fixed_color','sam2_remote') or not isinstance(group,dict)
            or group.get('primary') not in prompts or not isinstance(group.get('members'),list)
            or group['primary'] not in group['members'] or not set(group['members'])<=set(prompts)):
        raise ValueError('invalid_fixture_tracking_group')
    if tracking is not None and (type(tracking) is not int or not 0<tracking<=256):
        raise ValueError('bounded_color_tracking_padding_required')
    regions=config.get('mask_regions',{})
    if not isinstance(regions,dict) or not set(regions)<=set(prompts):
        raise ValueError('mask_measurement_region_identity_mismatch')
    from .perception import measurement_region
    for specification in regions.values():measurement_region(np.zeros((1,1),bool),specification)
    fixture_segmenter=None
    if backend=='sam2_remote':
        from .perception import FixedColorSegmenter
        targets=set(config['sam_objects'])
        if not targets or not targets<=set(prompts):raise ValueError('sam_target_identity_required')
        streaming=config.get('sam_streaming_objects')
        if streaming is not None and (not isinstance(streaming,list) or not set(streaming)<=targets):
            raise ValueError('sam_streaming_identity_required')
        profiles={k:v for k,v in config.get('color_profiles',{}).items() if k not in targets}
        if set(profiles)!=set(prompts)-targets:raise ValueError('fixture_mask_profiles_required')
        if group and targets.intersection(group['members']) and not set(group['members'])<=targets:
            raise ValueError('mixed_fixture_tracking_backends')
        if profiles:fixture_segmenter=FixedColorSegmenter(profiles)
    points=config.get('point_prompts',{})
    if not isinstance(points,dict) or not set(points)<=set(prompts):raise ValueError('point_prompt_identity_mismatch')
    from .perception import validate_point_prompt
    point_seeds={}
    for key,value in points.items():
        coordinates,labels=validate_point_prompt(value,config['image_shape'][:2])
        point_seeds[key]=dict(points=coordinates,labels=labels,box=np.asarray(prompts[key],float))
    return dict(config=config,backend=backend,segmenter=segmenter,prompts=prompts,
                point_seeds=point_seeds,
                sam_seeded=set(),
                visual_prompts={},visual_diagnostics={},
                tracking_generation={key:uuid4().hex for key in prompts},
                frame_source=None,tracking_results={},
                fixture_segmenter=fixture_segmenter,optional=optional,forwarded=forwarded,
                context_regions=context_regions)


class ConfiguredMaskProvider:
    """One implementation for local fixed-color and remote mask execution."""
    def __init__(self,config,*,segmenter=None,fixed_only=False):
        from .contracts import digest
        self.configuration_sha256=digest(config)
        if 'views' in config:
            configs=config['views']
            if (not isinstance(configs,dict) or not configs or
                    any(not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}',name) for name in configs)):
                raise ValueError('named_mask_views_required')
            self.default=config.get('default_view')
            if self.default is not None and self.default not in configs:raise ValueError('unknown_default_mask_view')
        else:self.default='default';configs={self.default:config}
        if fixed_only and any(value.get('backend')!='fixed_color' for value in configs.values()):
            raise ValueError('local_provider_requires_explicit_fixed_color')
        if isinstance(segmenter,dict) and set(segmenter)!=set(configs):raise ValueError('segmenter_view_identity_mismatch')
        self.states={name:_view_state(value,segmenter.get(name) if isinstance(segmenter,dict) else segmenter)
                     for name,value in configs.items()}
        self.lock=threading.Lock()
        # Each view owns its prompts and its HTTP client. Waiting for one SAM
        # response must not serialize independent camera work. An explicitly
        # shared segmenter still gets a shared lock (local SAM is stateful).
        segmenter_locks={}
        self.view_locks={name:segmenter_locks.setdefault(id(state['segmenter']),threading.RLock())
                         for name,state in self.states.items()}
        self.pending_tracking=None

    @contextmanager
    def _locked_views(self,views=None):
        selected=self.states if views is None else views
        locks={id(self.view_locks[name]):self.view_locks[name] for name in selected
               if name in self.view_locks}
        # Always acquire view locks before the short bookkeeping lock, and in
        # one order. A transaction commit waits for both camera jobs to finish.
        with ExitStack() as stack:
            for identity in sorted(locks):stack.enter_context(locks[identity])
            yield

    def begin_tracking_frame(self):
        """Stage local color prompts until downstream geometry accepts them.

        SAM model state is not covered by this transaction. Remote providers
        retain their existing behavior; no new runtime admission is introduced.
        """
        with self._locked_views(),self.lock:
            if any(s['backend'] not in ('fixed_color','sam2_remote') for s in self.states.values()):return None
            if self.pending_tracking is not None:raise RuntimeError('mask_tracking_frame_already_active')
            token=object()
            self.pending_tracking=(token,{name:dict(state,sam_seeded=set(state['sam_seeded']),
                prompts={key:list(box) for key,box in state['prompts'].items()})
                for name,state in self.states.items()})
            return token

    def discard_tracking_view(self,view):
        selected=view if view in self.states else self.default
        with self._locked_views([selected]),self.lock:
            if self.pending_tracking is None:return
            if selected not in self.states:raise ValueError('unknown_or_missing_mask_view')
            self.pending_tracking[1][selected]['prompts']={key:list(box) for key,box in self.states[selected]['prompts'].items()}
            self.pending_tracking[1][selected]['sam_seeded']=set(self.states[selected]['sam_seeded'])

    def finish_tracking_frame(self,token,*,accepted_views):
        with self._locked_views(),self.lock:
            if self.pending_tracking is None or token is not self.pending_tracking[0]:
                raise RuntimeError('mask_tracking_frame_identity_changed')
            selected=set(self.states) if accepted_views is None else {
                view if view in self.states else self.default for view in accepted_views}
            if not selected<=set(self.states):raise ValueError('unknown_or_missing_mask_view')
            for name in selected:
                self.states[name]['prompts']=self.pending_tracking[1][name]['prompts']
                self.states[name]['sam_seeded']=self.pending_tracking[1][name]['sam_seeded']
            self.pending_tracking=None

    def __call__(self,rgb,view=None):
        selected=self.default if view is None else view
        with self._locked_views([selected]):
            with self.lock:
                if selected not in self.states:raise ValueError('unknown_or_missing_mask_view')
                state=(self.pending_tracking[1] if self.pending_tracking is not None else self.states)[selected]
            try:return _segment_state(state,rgb)
            except ValueError as exc:
                raise ValueError('mask_view='+str(selected)+':'+str(exc)) from exc

    def is_sam_seeded(self,view,key):
        selected=self.default if view is None else view
        with self._locked_views([selected]),self.lock:
            if selected not in self.states:return False
            state=(self.pending_tracking[1] if self.pending_tracking is not None else self.states)[selected]
            return key in state['sam_seeded']

    def prompt_padding_px(self,view):
        selected=self.default if view is None else view
        with self._locked_views([selected]),self.lock:
            if selected not in self.states:return 0
            return int(self.states[selected]['config'].get('prompt_padding_px',0))

    def segment_frame(self,rgb,*,view,source,boxes=None,exclusion_boxes=None):
        """One source-bound request; geometry transactions never rewind video memory."""
        with self._locked_views([view]):
            with self.lock:
                if view not in self.states:raise ValueError('unknown_or_missing_mask_view')
                state=(self.pending_tracking[1] if self.pending_tracking is not None else self.states)[view]
            state['frame_source']=source
            try:return _segment_state(state,rgb,search_boxes=boxes,exclusion_boxes=exclusion_boxes)
            except ValueError as exc:raise ValueError('mask_view='+str(view)+':'+str(exc)) from exc
            finally:state['frame_source']=None

    def close(self):
        with self._locked_views():
            for state in self.states.values():
                close=getattr(state['segmenter'],'close',None)
                if close is not None:close()

    def tracking_snapshot(self):
        """Diagnostic pixel search state only; never model weights or credentials."""
        with self._locked_views(),self.lock:
            return {name:dict(backend=state['backend'],
                prompts={key:list(box) for key,box in state['prompts'].items()},
                visual_prompts={key:list(box) for key,box in state['visual_prompts'].items()},
                visual_state_is_motion_evidence=False)
                for name,state in self.states.items()}

    def diagnostic_snapshot(self):
        with self._locked_views(),self.lock:
            return {name:dict(state['visual_diagnostics']) for name,state in self.states.items()}

    def reset_visual_tracking(self):
        """Discard search-only associations when acquisition/task identity changes."""
        with self._locked_views(),self.lock:
            from uuid import uuid4
            for name,state in self.states.items():
                state['visual_prompts'].clear()
                state['visual_diagnostics'].clear()
                state['tracking_results'].clear()
                state['sam_seeded'].clear()
                state['tracking_generation'].update({key:uuid4().hex for key in state['prompts']})
                if self.pending_tracking is not None:
                    self.pending_tracking[1][name]['sam_seeded'].clear()

    def registered_entities(self,view):
        if view not in self.states:raise ValueError('unknown_or_missing_mask_view')
        return tuple(self.states[view]['prompts'])

    def set_search_hint(self,*,view,object_id,box,point_prompt=None):
        """Replace one pixel prompt before a new frame's geometry transaction.

        The caller binds an issued image and task scope. This method accepts no
        masks, poses or robot state, and does not mark any observation as valid.
        """
        with self._locked_views([view]),self.lock:
            if view not in self.states:raise ValueError('unknown_or_missing_mask_view')
            state=self.states[view]
            if state['backend'] not in ('fixed_color','sam2_remote') or object_id not in state['prompts']:
                raise ValueError('unregistered_search_hint')
            if self.pending_tracking is not None:raise ValueError('mask_tracking_frame_already_active')
            if not isinstance(box,(list,tuple)) or len(box)!=4 or any(type(v) is not int for v in box):
                raise ValueError('integer_search_hint_box_required')
            x0,y0,x1,y1=box;height,width=state['config']['image_shape'][:2]
            if not 0<=x0<x1<=width or not 0<=y0<y1<=height:
                raise ValueError('fixed_search_box_outside_ROI')
            seed=None
            if point_prompt is not None:
                from .perception import validate_point_prompt
                points,labels=validate_point_prompt(point_prompt,(height,width))
                seed=dict(points=points,labels=labels,box=np.asarray(box,float))
            state['prompts'][object_id]=list(box)
            if seed is None:state['point_seeds'].pop(object_id,None)
            else:state['point_seeds'][object_id]=seed
            state['visual_prompts'].pop(object_id,None)
            from uuid import uuid4
            state['tracking_generation'][object_id]=uuid4().hex
            if state['backend']=='sam2_remote' and object_id in state['config']['sam_objects']:
                state['sam_seeded'].add(object_id)

    def seed_issued_image(self, *, view, object_id, rgb, source):
        """Bind a prompt to the image the planner saw, before tracking new RGB.

        Historical pixels initialize visual memory only; they never supply
        current depth, a scene pose, or attachment evidence.
        """
        with self._locked_views([view]):
            state=self.states[view]
            state['frame_source']=source
            try:
                if not _streaming_object(state,object_id):return False
                mask,score=_segment_box(state,rgb,state['prompts'][object_id],object_id,reseed=True)
                if _mask_missing(state,object_id,np.asarray(mask,bool),score):
                    state['sam_seeded'].discard(object_id)
                    raise ValueError('mask_uncertain_requires_new_prompt:issued_image')
                return True
            finally:state['frame_source']=None

    def with_search_boxes(self,rgb,*,view,boxes):
        """Trusted local geometry can supply hints, never masks or object poses."""
        with self._locked_views([view]):
            with self.lock:
                if view not in self.states:raise ValueError('unknown_or_missing_mask_view')
                state=(self.pending_tracking[1] if self.pending_tracking is not None else self.states)[view]
                if state['backend'] not in ('fixed_color','sam2_remote') or not set(boxes)<=set(state['prompts']):
                    raise ValueError('unregistered_search_hint')
            try:return _segment_state(state,rgb,search_boxes=boxes)
            except ValueError as exc:raise ValueError('mask_view='+str(view)+':'+str(exc)) from exc


def _sam_arguments(state,rgb,box,key):
    arguments={'box':np.asarray(box,float)}
    if key in state['point_seeds']:
        seed=state['point_seeds'][key];before=seed['box'];after=arguments['box']
        coordinates=(seed['points']-before[:2])/(before[2:]-before[:2])*(after[2:]-after[:2])+after[:2]
        coordinates=np.clip(coordinates,[0,0],[rgb.shape[1]-1,rgb.shape[0]-1])
        arguments.update(points=coordinates,labels=seed['labels'])
    return arguments


def _streaming_object(state,key):
    cfg=state['config']
    return (cfg.get('sam_streaming',False) and state['frame_source'] is not None
            and key in cfg.get('sam_streaming_objects',cfg.get('sam_objects',[])))


def _segment_box(state,rgb,box,key,*,reseed=False,discover=False):
    cfg=state['config'];segmenter=state['segmenter']
    fixed=(state['backend']=='fixed_color' or state['backend']=='sam2_remote' and key not in cfg['sam_objects'])
    if state['backend']=='sam2_remote' and fixed:segmenter=state['fixture_segmenter']
    arguments=({'box':np.asarray(box,float),'object_id':key} if fixed else _sam_arguments(state,rgb,box,key))
    previous_tracking=state['tracking_results'].pop(key,None) or {}
    if not fixed and _streaming_object(state,key):
        from .contracts import digest
        source=state['frame_source']
        if type(source.get('source_ns')) is not int:raise ValueError('sam_source_timestamp_missing')
        stream_id=digest(dict(generation=state['tracking_generation'][key],stream=source['stream']))
        if discover:
            (mask,score),metadata=segmenter.discover(rgb,stream_id=stream_id,
                object_id=key,source_ns=source['source_ns'])
        else:
            (mask,score),metadata=segmenter.track(rgb,**arguments,stream_id=stream_id,
                object_id=key,source_ns=source['source_ns'],reseed=reseed)
        metadata=dict(metadata)
        if metadata.get('reseeded'):
            metadata['seed_provenance']=dict(source_ns=source['source_ns'],stream_id=stream_id,
                **{k:v for k,v in metadata.items() if k.startswith(('seed_','semantic_','identity_'))})
        elif previous_tracking.get('stream_id')==metadata.get('stream_id'):
            metadata['seed_provenance']=previous_tracking.get('seed_provenance')
        if (previous_tracking.get('stream_id')==metadata.get('stream_id')
                and previous_tracking.get('source_ns')==metadata.get('source_ns')):
            for name in ('seed_inference_ms','seed_inference_count','tracking_inference_ms','tracking_inference_count'):
                if name in metadata:metadata[name]+=previous_tracking.get(name,0)
        state['tracking_results'][key]=metadata
    else:
        mask,score=segmenter.segment(rgb,**arguments)[0]
    # Tracking and explicit hints share the same bounded recovery. Geometry still
    # decides whether this new image measurement may update tracking state.
    recover=getattr(segmenter,'recover_truncated',None)
    if (fixed and recover is not None
            and (score<cfg['minimum_score'] or np.asarray(mask,bool).sum()<cfg['minimum_area_px'])):
        mask,score=recover(rgb,box=np.asarray(box,float),object_id=key,
            padding=cfg.get('fixed_color_tracking_padding_px',cfg['prompt_padding_px']))[0]
    return mask,score


def _mask_missing(state,key,mask,score):
    # Temporal presence is not image-mask IoU. A nonempty propagated mask is
    # passed to actual depth/identity geometry; presence is logged separately.
    # Do not apply the old .9 image-quality threshold to a different quantity.
    tracking=state['tracking_results'].get(key)
    sam=(state['backend']=='sam2' or state['backend']=='sam2_remote'
         and key in state['config']['sam_objects'])
    return (tracking is not None and tracking.get('identity_status')=='unresolved' or
        np.asarray(mask,bool).sum()<state['config']['minimum_area_px'] or
        (not tracking and not sam and score<state['config']['minimum_score']))


def _segment_state(state,rgb,search_boxes=None,exclusion_boxes=None):
    cfg=state['config'];backend=state['backend'];segmenter=state['segmenter']
    # A group relocation is provisional until every required mask, measurement
    # region and identity check has passed. Failed frames must not poison the
    # next frame's search origin (including failures after primary detection).
    prompts=dict(state['prompts']);optional=state['optional'];forwarded=state['forwarded']
    # Pixel associations are search state, not an accepted 3-D scene. A bad
    # depth fit or missing aperture must not rewind the block to an old box.
    # Transaction copies deliberately share these per-view dictionaries.
    if backend=='sam2_remote':prompts.update(state['visual_prompts'])
    if rgb.dtype!=np.uint8 or tuple(rgb.shape)!=tuple(cfg['image_shape']):raise ValueError('ROI_image_contract')
    masks={};identity_masks={};next_prompts={}
    group=cfg.get('fixture_tracking_group')
    sam_fixture=bool(backend=='sam2_remote' and group and set(group['members'])<=set(cfg['sam_objects']))
    if backend in ('fixed_color','sam2_remote') and group and not sam_fixture:
        primary=group['primary'];keys=group['members'];box=np.asarray(prompts[primary],float)
        mask,score=_segment_box(state,rgb,box,primary)
        if score<cfg['minimum_score']:
            search=cfg.get('reacquisition_boxes',{}).get(primary)
            if search is not None:mask,score=_segment_box(state,rgb,search,primary)
        if score>=cfg['minimum_score'] and mask.sum()>=cfg['minimum_area_px']:
            y,x=np.nonzero(mask);shift=np.array([x.mean(),y.mean()])-(box[:2]+box[2:])/2
            # Move the registered aperture group together; individual detections
            # and current RGB-D fixture geometry still have to agree.
            for key in keys:
                b=np.asarray(prompts[key],float)+np.tile(shift,2)
                b[[0,2]]=np.clip(b[[0,2]],0,rgb.shape[1]);b[[1,3]]=np.clip(b[[1,3]],0,rgb.shape[0])
                prompts[key]=b.tolist()
    selected={};discover=set()
    for key,box in prompts.items():
        if (backend=='sam2_remote' and key in cfg['sam_objects']
                and cfg.get('require_explicit_target_prompt',True) and key not in state['sam_seeded']):
            if key in (search_boxes or {}):box=search_boxes[key]
            elif (_streaming_object(state,key) and callable(getattr(segmenter,'discover',None))):
                # Only the semantic service may initialize an unknown target
                # without an image-bound hint. Old configured boxes are not a
                # fallback for identity; discovery searches the current image.
                box=[0,0,rgb.shape[1],rgb.shape[0]];discover.add(key)
            elif key in optional:continue
            else:raise ValueError('mask_uncertain_requires_new_prompt:object='+key)
        if backend=='sam2_remote' and key in cfg['sam_objects'] and key in (search_boxes or {}):
            # The current camera transform predicts where to search after arm
            # motion. Trying yesterday's pixel box first can confidently segment
            # a different surface. This projection still supplies no pose proof.
            projected=np.asarray(search_boxes[key],float)
            tracked=state['visual_prompts'].get(key)
            # A collision-margin projection can be much larger than the
            # visible object. Keep a wholly contained visual mask box tight;
            # otherwise use the motion prediction, with the broader projection
            # still available for one bounded reacquisition below.
            tight=np.asarray(tracked,float) if tracked is not None else None
            contained=(tight is not None and np.all(tight[:2]>=projected[:2])
                       and np.all(tight[2:]<=projected[2:]))
            box=tracked if contained else search_boxes[key]
        selected[key]=box
    # Static fixture masks share one uploaded image/encoder pass. Moving objects
    # keep their temporal memory, and recovery still gets one explicit retry.
    batch={key:_sam_arguments(state,rgb,box,key) for key,box in selected.items()
        if backend=='sam2_remote' and key in cfg['sam_objects'] and not _streaming_object(state,key)}
    results={}
    if len(batch)>1 and callable(getattr(segmenter,'segment_many',None)):
        results=segmenter.segment_many(rgb,prompts=batch)
        if set(results)!=set(batch):raise ValueError('sam_response_entities_changed')
        for key in batch:state['tracking_results'].pop(key,None)
    for key,box in selected.items():
        mask,score=results[key] if key in results else _segment_box(state,rgb,box,key,discover=key in discover)
        mask=np.asarray(mask,bool)
        attempted_boxes=[np.asarray(box)]
        if mask.shape!=rgb.shape[:2]:raise ValueError('mask_dimensions_changed')
        if (_mask_missing(state,key,mask,score) and key in (search_boxes or {})
                and not any(np.array_equal(search_boxes[key],previous) for previous in attempted_boxes)):
            mask,score=_segment_box(state,rgb,search_boxes[key],key,reseed=True)
            attempted_boxes.append(np.asarray(search_boxes[key]))
            mask=np.asarray(mask,bool)
            if mask.shape!=rgb.shape[:2]:raise ValueError('mask_dimensions_changed')
        tracking=state['tracking_results'].get(key) or {}
        fixture_exclusion_diag=None
        if (_mask_missing(state,key,mask,score) and key in discover
                and tracking.get('identity_status')=='unresolved'
                and key in (exclusion_boxes or {})):
            ex=np.asarray(exclusion_boxes[key],float)
            total=int(tracking.get('semantic_candidates',0))
            summary=list(tracking.get('semantic_candidate_summary') or [])
            truncated=bool(tracking.get('semantic_candidate_summary_truncated',False))
            fixture_exclusion_diag=dict(
                exclusion_box=[int(round(v)) for v in ex.tolist()],
                total_candidates=total,
                summary_count=len(summary),
                truncated=truncated,
                applied=False,
                excluded_candidates=0,
                remaining_candidates=len(summary),
                semantic_candidate_summary=summary,
            )
            if total>=2 and len(summary)==total and not truncated:
                remaining=[]
                for cand in summary:
                    cbox=cand.get('box')
                    if not isinstance(cbox,(list,tuple)) or len(cbox)!=4:
                        remaining.append(cand);continue
                    cx=(float(cbox[0])+float(cbox[2]))/2.0
                    cy=(float(cbox[1])+float(cbox[3]))/2.0
                    if ex[0]<=cx<=ex[2] and ex[1]<=cy<=ex[3]:
                        continue
                    remaining.append(cand)
                excluded_count=len(summary)-len(remaining)
                fixture_exclusion_diag.update(
                    applied=True,
                    excluded_candidates=excluded_count,
                    remaining_candidates=len(remaining),
                )
                if len(remaining)==1:
                    cand=remaining[0]
                    cand_score=cand.get('score')
                    if cand_score is not None and float(cand_score)<cfg['minimum_score']:
                        fixture_exclusion_diag['rejected_below_minimum_score']=True
                    else:
                        pad=int(cfg.get('sam_tracking_padding_px',8))
                        cbox=np.asarray(cand['box'],float)
                        reseed_box=np.asarray([cbox[0]-pad,cbox[1]-pad,cbox[2]+pad,cbox[3]+pad],float)
                        reseed_box=np.clip(reseed_box,[0,0,0,0],[rgb.shape[1],rgb.shape[0],rgb.shape[1],rgb.shape[0]])
                        if (reseed_box[2]>reseed_box[0] and reseed_box[3]>reseed_box[1]
                                and not any(np.array_equal(reseed_box,previous) for previous in attempted_boxes)):
                            mask,score=_segment_box(state,rgb,reseed_box,key,reseed=True)
                            attempted_boxes.append(reseed_box);mask=np.asarray(mask,bool)
                            if mask.shape!=rgb.shape[:2]:raise ValueError('mask_dimensions_changed')
                            tracking=state['tracking_results'].get(key) or {}
                            if float(score)<cfg['minimum_score']:
                                fixture_exclusion_diag['rejected_below_minimum_score']=True
                                if key in state['tracking_results']:
                                    state['tracking_results'][key]['identity_status']='unresolved'
                                state['sam_seeded'].discard(key)
                            elif key in state['tracking_results']:
                                state['tracking_results'][key].update(
                                    recovery_search_source='fixture_exclusion_unique_candidate',
                                    fixture_exclusion_excluded_candidates=excluded_count,
                                    fixture_exclusion_box=fixture_exclusion_diag['exclusion_box'],
                                )
        tracking=state['tracking_results'].get(key) or {}
        if (_mask_missing(state,key,mask,score) and tracking.get('identity_status')=='unresolved'
                and tracking.get('semantic_candidates',0)>0 and tracking.get('semantic_scoped_candidates')==0):
            # A partly occluded mask can shrink its next search box so far that
            # the same object's centre no longer fits when it reappears. Reuse
            # the measured semantic seed extent once, not the configured old
            # ROI or an arbitrary padding/score threshold. Current RGB-D must
            # still establish geometry; these old pixels only bound search.
            seed=tracking.get('seed_provenance') or {}
            candidate=next((c for c in seed.get('semantic_candidate_summary',[])
                if c['index']==seed.get('semantic_selected_index')),None)
            if candidate is not None:
                old=np.asarray(candidate['box'],float);current=np.asarray(box,float)
                search=np.r_[np.minimum(old[:2],current[:2]),np.maximum(old[2:],current[2:])]
                search=np.clip(search,[0,0,0,0],[rgb.shape[1],rgb.shape[0],rgb.shape[1],rgb.shape[0]])
                if not any(np.array_equal(search,previous) for previous in attempted_boxes):
                    mask,score=_segment_box(state,rgb,search,key,reseed=True)
                    attempted_boxes.append(search);mask=np.asarray(mask,bool)
                    if mask.shape!=rgb.shape[:2]:raise ValueError('mask_dimensions_changed')
                    state['tracking_results'][key].update(recovery_search_source='previous_semantic_seed_bounds',
                        recovery_seed_source_ns=seed.get('source_ns'))
        if (_mask_missing(state,key,mask,score) and key not in discover
                and backend in ('fixed_color','sam2_remote')):
            search=cfg.get('reacquisition_boxes',{}).get(key)
            if search is not None and not any(np.array_equal(search,previous) for previous in attempted_boxes):
                mask,score=_segment_box(state,rgb,search,key,reseed=True)
                attempted_boxes.append(np.asarray(search))
                mask=np.asarray(mask,bool)
                if mask.shape!=rgb.shape[:2]:raise ValueError('mask_dimensions_changed')
        state['visual_diagnostics'][key]=dict(score=float(score),area_px=int(mask.sum()),
            minimum_score=cfg['minimum_score'],minimum_area_px=cfg['minimum_area_px'],
            attempted_boxes=[np.asarray(b).tolist() for b in attempted_boxes],
            tracking=state['tracking_results'].get(key),
            **({'fixture_exclusion':fixture_exclusion_diag} if fixture_exclusion_diag is not None else {}),
            quality_warning=('mask_quality_below_reference' if not state['tracking_results'].get(key)
                and backend in ('sam2','sam2_remote') and score<cfg['minimum_score'] else None),
            score_kind='object_presence_not_mask_iou' if state['tracking_results'].get(key) else 'mask_quality',
            search_only=True,motion_evidence=False)
        if _mask_missing(state,key,mask,score):
            if key in optional:continue
            raise ValueError('mask_uncertain_requires_new_prompt:object='+key)
        segmentation_mask=mask
        if key in cfg.get('mask_regions',{}):
            from .perception import measurement_region
            mask=measurement_region(mask,cfg['mask_regions'][key])
            if mask.sum()<cfg['minimum_area_px']:raise ValueError('measurement_region_too_small:object='+key)
        yy,xx=np.nonzero(mask)
        if min(xx)==0 or min(yy)==0 or max(xx)==rgb.shape[1]-1 or max(yy)==rgb.shape[0]-1:
            raise ValueError('mask_truncated:object='+key)
        # Depth samples may lie on a ring outside the segmented aperture.
        # Only the original segmentation pixels identify each entity.
        identity_masks[key]=segmentation_mask
        masks[key]=mask
        yy,xx=np.nonzero(segmentation_mask)
        padding=(cfg.get('sam_tracking_padding_px',8) if backend=='sam2_remote' and key in cfg['sam_objects']
                 else cfg.get('fixed_color_tracking_padding_px',cfg['prompt_padding_px']))
        # FixedColorSegmenter slices [x0:x1, y0:y1]; maxima are inclusive
        # pixel indices, so the upper edge needs +1 before adding padding.
        # SAM's external prompt convention remains unchanged here.
        edge=1 if backend in ('fixed_color','sam2_remote') else 0
        next_prompts[key]=[max(0,int(min(xx))-padding),max(0,int(min(yy))-padding),
            min(rgb.shape[1]-1+edge,int(max(xx))+edge+padding),
            min(rgb.shape[0]-1+edge,int(max(yy))+edge+padding)]
    ambiguous_optional=set()
    for ka,kb in combinations(identity_masks,2):
        # A lid/surface mask intentionally contains aperture pixels. It is a
        # context region, not a second object identity; do not suppress both
        # objects' search updates as if this were an ambiguous grasp target.
        if ka in state['context_regions'] or kb in state['context_regions']:continue
        diagnostics=state['visual_diagnostics']
        smaller_area=min(diagnostics[ka]['area_px'],diagnostics[kb]['area_px'])
        shared_px=int(np.count_nonzero(identity_masks[ka] & identity_masks[kb]))
        fraction=shared_px/max(1,smaller_area)
        if fraction>cfg['maximum_overlap_fraction']:
            for key,other in ((ka,kb),(kb,ka)):
                diagnostics[key].setdefault('identity_overlaps',[]).append(dict(
                    other=other,shared_segmentation_px=shared_px,smaller_area_px=smaller_area,
                    fraction=fraction))
            if frozenset((ka,kb)) not in forwarded:
                optional_pair={ka,kb}&optional
                if not optional_pair:raise ValueError('object_identity_overlap')
                # Reject ambiguous optional identities, not independent masks
                # from the entire camera. Never select an identity by score.
                ambiguous_optional.update(optional_pair)
            next_prompts.pop(ka,None);next_prompts.pop(kb,None)
    for key in ambiguous_optional:
        masks.pop(key,None);next_prompts.pop(key,None)
        state['visual_diagnostics'][key]['rejected_reason']='object_identity_overlap'
    if backend!='fixed_color' or cfg.get('fixed_color_tracking_padding_px') is not None:
        if group and not sam_fixture:
            # Ring masks are expanded measurement regions, not new segmentation
            # boxes. Feeding them back grows boxes into neighbouring apertures.
            next_prompts={k:v for k,v in next_prompts.items() if k not in group['members']}
        prompts.update(next_prompts)
    if backend=='sam2_remote':
        state['visual_prompts'].update({k:list(v) for k,v in next_prompts.items()
            if k in cfg['sam_objects']})
    state['prompts'].update(prompts)
    if backend=='sam2_remote':state['sam_seeded'].update(set(masks)&set(cfg['sam_objects']))
    return masks


def scene_mask_provider(scene,site):
    """Explicit deployment mode; local baseline cannot silently fall back to a model."""
    mode=scene.get('mask_execution','remote')
    if mode=='remote':return MaskClient(scene['mask_url'],timeout=scene['mask_timeout_seconds'])
    if mode not in ('local_fixed_color','local_sam2'):raise ValueError('unknown_mask_execution_mode')
    from pathlib import Path
    from hashlib import sha256
    site=Path(site)
    if scene.get('local_mask_config')!='masks.local.json':raise ValueError('registered_local_mask_config_required')
    path=site/'masks.local.json';record=site/'development.local.json'
    if path.is_symlink() or not path.is_file() or record.is_symlink() or not record.is_file():
        raise ValueError('local_mask_regular_reviewed_config_required')
    data=path.read_bytes();reviewed=json.loads(record.read_text()).get('reviewed_files',{})
    if sha256(data).hexdigest()!=reviewed.get(path.name):raise ValueError('local_mask_configuration_identity_changed')
    config=json.loads(data)
    if mode=='local_sam2' and any(view.get('backend')!='sam2_remote' for view in config['views'].values()):
        raise ValueError('sam_mode_requires_sam_no_color_fallback')
    return ConfiguredMaskProvider(config,fixed_only=mode=='local_fixed_color')


def mask_server(config,*,segmenter=None):
    # Each view owns its dimensions, identities, prompts and tracker state. Views
    # cannot borrow prompts merely because their input arrays have the same shape.
    provider=ConfiguredMaskProvider(config,segmenter=segmenter)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup();self.connection.settimeout(3)
        def log_message(self,*args):pass
        def do_POST(self):
            view=None;key=None
            try:
                length=int(self.headers.get('Content-Length','0'))
                if self.path!='/segment' or not 0<length<=16*1024*1024:raise ValueError('invalid_image_request')
                view=self.headers.get('X-Harness-View',provider.default)
                rgb=np.load(io.BytesIO(self.rfile.read(length)),allow_pickle=False)
                masks=provider(rgb,view=view)
                stream=io.BytesIO();np.savez_compressed(stream,**masks)
                payload=stream.getvalue()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)))
                self.send_header('X-Harness-View',view);self.end_headers();self.wfile.write(payload)
            except Exception as exc:
                reason=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__
                if ':object=' in reason:key=reason.rsplit(':object=',1)[-1]
                payload=json.dumps({'error':'segmentation_requires_review','reason':reason,
                                    'view':view,'object_id':key}).encode()
                self.send_response(422);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=HTTPServer(('127.0.0.1',config['port']),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
