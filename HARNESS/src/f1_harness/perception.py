"""Local segmentation; providers share full ROI pixel coordinates."""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import HEAD_ROI


def roi_image(image, roi=HEAD_ROI):
    x0, y0, x1, y1 = roi
    if not (0 <= x0 < x1 <= image.shape[1] and 0 <= y0 < y1 <= image.shape[0]):
        raise ValueError("ROI outside image")
    return image[y0:y1, x0:x1].copy()


def measurement_region(mask, specification):
    """A detected aperture may reference its visible surrounding lid, not its void.

    The ring is generated only from an actually detected mask. It does not fill
    a missing aperture or infer depth where the RGB-D sensor has none.
    """
    if not isinstance(specification,dict) or specification.get('kind')!='outer_ring':
        raise ValueError('unsupported_mask_measurement_region')
    inner,outer=specification.get('inner_px'),specification.get('outer_px')
    if type(inner) is not int or type(outer) is not int or not 0<=inner<outer<=32:
        raise ValueError('bounded_measurement_ring_required')
    mask=np.asarray(mask,bool)
    if mask.ndim!=2:raise ValueError('measurement_mask_must_be_2d')
    if not mask.any():return mask.copy()
    foreground=mask.astype(np.uint8)
    larger=cv2.dilate(foreground,np.ones((2*outer+1,2*outer+1),np.uint8))
    smaller=cv2.dilate(foreground,np.ones((2*inner+1,2*inner+1),np.uint8))
    return (larger>0)&(smaller==0)


class ContourSegmenter:
    """Explicit foreground mask in; simple shape candidates out. No semantic certainty from color."""
    def segment(self, foreground, *, min_area_px=100):
        mask = np.asarray(foreground, np.uint8)
        if mask.ndim != 2:
            raise ValueError("foreground must be a 2D mask")
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        results = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area_px:
                continue
            perimeter = cv2.arcLength(contour, True)
            polygon = cv2.approxPolyDP(contour, .025 * perimeter, True)
            x, y, w, h = cv2.boundingRect(contour)
            clipped = x == 0 or y == 0 or x + w >= mask.shape[1] or y + h >= mask.shape[0]
            shape = "unknown"
            if len(polygon) == 3:
                shape = "triangle"
            elif len(polygon) == 4:
                (_, _), (a, b), _ = cv2.minAreaRect(contour)
                if min(a, b) > 0 and max(a, b) / min(a, b) < 1.2:
                    shape = "square"
            elif perimeter and 4 * np.pi * area / perimeter ** 2 > .8:
                shape = "circle"
            component = np.zeros_like(mask)
            cv2.drawContours(component, [contour], -1, 255, cv2.FILLED)
            results.append(dict(shape=shape, bbox=[x, y, w, h], truncated=clipped,
                                mask=component.astype(bool), area_px=float(area)))
        return results


class FixedColorSegmenter:
    """Measured, explicit color profiles for a fixed baseline, not general object recognition.

    Each target must have exactly one component within its bounded search box. Colors,
    areas and search regions are scene parameters and require separate saved-frame validation.
    """
    def __init__(self,profiles):
        if not profiles:raise ValueError('fixed_color_profiles_required')
        self.profiles=profiles
        for profile in profiles.values():
            lower=np.asarray(profile['hsv_lower']);upper=np.asarray(profile['hsv_upper'])
            if (lower.shape!=(3,) or upper.shape!=(3,) or not np.all(np.isfinite(lower))
                    or not np.all(np.isfinite(upper)) or np.any(lower<0) or np.any(upper>[179,255,255])
                    or np.any(lower>upper) or not 0<profile['minimum_area_px']<=profile['maximum_area_px']):
                raise ValueError('invalid_fixed_color_profile')

    def _components(self,rgb,box,object_id):
        p=self.profiles[object_id]
        bounds=np.asarray(box,float)
        if bounds.shape!=(4,) or not np.all(np.isfinite(bounds)):raise ValueError('fixed_search_box_required')
        x0,y0,x1,y1=np.rint(bounds).astype(int)
        if not 0<=x0<x1<=rgb.shape[1] or not 0<=y0<y1<=rgb.shape[0]:raise ValueError('fixed_search_box_outside_ROI')
        hsv=cv2.cvtColor(rgb[y0:y1,x0:x1],cv2.COLOR_RGB2HSV)
        foreground=cv2.inRange(hsv,np.asarray(p['hsv_lower'],np.uint8),np.asarray(p['hsv_upper'],np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(foreground,connectivity=8)
        candidates=[i for i in range(1,count) if p['minimum_area_px']<=stats[i,cv2.CC_STAT_AREA]<=p['maximum_area_px']]
        return (x0,y0,x1,y1),labels,stats,candidates

    def segment(self,rgb,*,box,object_id):
        (x0,y0,x1,y1),labels,stats,candidates=self._components(rgb,box,object_id)
        if len(candidates)!=1:
            # No confidence or arbitrary winner when the scene no longer identifies one target.
            return [(np.zeros(rgb.shape[:2],bool),0.)]
        i=candidates[0];x,y,w,h,_=stats[i]
        if x==0 or y==0 or x+w>=x1-x0 or y+h>=y1-y0:
            return [(np.zeros(rgb.shape[:2],bool),0.)]
        mask=np.zeros(rgb.shape[:2],bool);mask[y0:y1,x0:x1]=labels==i
        return [(mask,1.)]  # Deterministic gate pass; not a learned probability.

    def recover_truncated(self,rgb,*,box,object_id,padding):
        """Finish one partially seen component without changing its identity.

        A search-window edge is not an image edge. Expand only when a unique
        area-qualified seed was cut by that window, keeping its original pixels
        and window as identity constraints. Other components entering the seed
        window remain ambiguous; no largest-component selection is performed.
        """
        if type(padding) is not int or not 0<padding<=256:
            raise ValueError('bounded_color_tracking_padding_required')
        empty=[(np.zeros(rgb.shape[:2],bool),0.)]
        original,labels,stats,candidates=self._components(rgb,box,object_id)
        if len(candidates)!=1:return empty
        ox0,oy0,ox1,oy1=original
        seed=labels==candidates[0]
        x,y,w,h,_=stats[candidates[0]]
        if x>0 and y>0 and x+w<ox1-ox0 and y+h<oy1-oy0:return empty
        bounds=original;margin=padding
        while True:
            expanded=(max(0,ox0-margin),max(0,oy0-margin),
                      min(rgb.shape[1],ox1+margin),min(rgb.shape[0],oy1+margin))
            if expanded==bounds:return empty
            bounds,labels,stats,candidates=self._components(rgb,expanded,object_id)
            x0,y0,x1,y1=bounds
            seed_window=labels[oy0-y0:oy1-y0,ox0-x0:ox1-x0]
            matching=[i for i in candidates if np.any(seed_window==i)]
            if len(matching)!=1 or not np.all(seed_window[seed]==matching[0]):return empty
            i=matching[0];x,y,w,h,_=stats[i]
            if x>0 and y>0 and x+w<x1-x0 and y+h<y1-y0:
                mask=np.zeros(rgb.shape[:2],bool);mask[y0:y1,x0:x1]=labels==i
                return [(mask,1.)]
            # Real image/ROI clipping cannot be repaired by enlarging a prompt.
            if x+x0==0 or y+y0==0 or x+x0+w==rgb.shape[1] or y+y0+h==rgb.shape[0]:return empty
            margin*=2


def validate_point_prompt(value, shape):
    """Bound positive/negative pixel prompts to this actual image."""
    if not isinstance(value,dict) or set(value)!={'points','labels'}:
        raise ValueError('sam_points_invalid')
    points=np.asarray(value['points'],float);labels=np.asarray(value['labels'])
    if (points.ndim!=2 or points.shape[1]!=2 or not 1<=len(points)<=32
            or labels.shape!=(len(points),) or labels.dtype.kind not in 'iu'
            or not np.isfinite(points).all() or not np.isin(labels,[0,1]).all()
            or np.any(points<0) or np.any(points[:,0]>=shape[1]) or np.any(points[:,1]>=shape[0])):
        raise ValueError('sam_points_invalid')
    return points,labels.astype(np.int32)


class Sam2Segmenter:
    """Optional isolated perception environment; weights/config must be pinned by deployment."""
    def __init__(self, model_config, checkpoint, *, device="cpu"):
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        self.predictor = SAM2ImagePredictor(build_sam2(model_config, str(checkpoint), device=device))
        self.device=device
        self.image_identity=None

    def segment(self, rgb, *, box=None, points=None, labels=None):
        import torch
        import hashlib
        from contextlib import nullcontext
        if box is None and points is None:
            raise ValueError("SAM2 needs an explicit object prompt")
        rgb=np.asarray(rgb,np.uint8)
        identity=(rgb.shape,hashlib.sha256(rgb.tobytes()).digest())
        autocast=torch.autocast('cuda',dtype=torch.bfloat16) if self.device.startswith('cuda') else nullcontext()
        with torch.inference_mode(),autocast:
            if identity!=self.image_identity:
                self.predictor.set_image(rgb)
                self.image_identity=identity
            masks, scores, _ = self.predictor.predict(point_coords=points, point_labels=labels,
                                                    box=box, multimask_output=True)
        order = np.argsort(scores)[::-1]
        return [(masks[i].astype(bool), float(scores[i])) for i in order]


class Sam2WindowTracker:
    """Bounded saved-frame window. Source stamps must advance; invalidation starts a new state."""
    def __init__(self,model_config,checkpoint,*,device='cpu'):
        from sam2.build_sam import build_sam2_video_predictor
        self.predictor=build_sam2_video_predictor(model_config,str(checkpoint),device=device)

    def track(self,video_directory,*,source_stamps,box,object_id=1,max_frames=16):
        import torch
        if not 0<len(source_stamps)<=max_frames or any(b<=a for a,b in zip(source_stamps,source_stamps[1:])):
            raise ValueError('tracking_requires_bounded_distinct_source_frames')
        results=[]
        with torch.inference_mode():
            state=self.predictor.init_state(str(video_directory),offload_video_to_cpu=True,offload_state_to_cpu=True)
            if state['num_frames']!=len(source_stamps):raise ValueError('tracking_frame_identity_mismatch')
            self.predictor.add_new_points_or_box(state,frame_idx=0,obj_id=object_id,box=np.asarray(box,float))
            for frame_idx,ids,logits in self.predictor.propagate_in_video(state):
                results.append(dict(frame_index=frame_idx,source_ns=source_stamps[frame_idx],
                    masks={int(key):(logits[i]>0).cpu().numpy().squeeze() for i,key in enumerate(ids)}))
        return results
