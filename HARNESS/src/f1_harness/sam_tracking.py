"""Incremental adapter for the pinned upstream SAM2VideoPredictor.

Only the frame loader and bounded storage live here; segmentation, temporal
memory and propagation are upstream SAM2. Results are pixel evidence, never
attachment, depth, or robot authority. Callers must serialize access.
"""
from collections import OrderedDict
from contextlib import nullcontext
import hashlib

import numpy as np


class Sam2StreamingTracker:
    def __init__(self, model_config, checkpoint, *, device='cpu', memory_frames=32,
                 max_tracks=16, predictor=None):
        if memory_frames < 32 or max_tracks < 1:
            raise ValueError('invalid_sam_storage_capacity')
        if predictor is None:
            from sam2.build_sam import build_sam2_video_predictor
            predictor = build_sam2_video_predictor(model_config, str(checkpoint), device=device)
        self.predictor = predictor
        self.device = device
        self.memory_frames = memory_frames
        self.max_tracks = max_tracks
        self.tracks = OrderedDict()

    def reset(self, stream_id=None):
        for key in list(self.tracks):
            if stream_id is None or key[0] == stream_id:
                del self.tracks[key]

    def _image(self, rgb):
        # Match upstream utils.misc._load_img_as_tensor exactly, without a
        # lossy JPEG round trip or loading a complete future video directory.
        import torch
        from PIL import Image
        pixels = np.asarray(Image.fromarray(rgb).resize(
            (self.predictor.image_size, self.predictor.image_size))) / 255.0
        tensor = torch.from_numpy(pixels).permute(2, 0, 1).float()
        mean = torch.tensor([.485, .456, .406])[:, None, None]
        std = torch.tensor([.229, .224, .225])[:, None, None]
        return (tensor - mean) / std

    def _state(self, rgb):
        # SAM2's public init_state requires a JPEG directory/video file. This
        # is its state contract with an incremental RGB loader; no tracking
        # algorithm or model internals are reimplemented.
        device = self.predictor.device
        return dict(images={0: self._image(rgb)}, num_frames=1,
            offload_video_to_cpu=True, offload_state_to_cpu=False,
            video_height=rgb.shape[0], video_width=rgb.shape[1],
            device=device, storage_device=device,
            point_inputs_per_obj={}, mask_inputs_per_obj={}, cached_features={},
            constants={}, obj_id_to_idx=OrderedDict(), obj_idx_to_id=OrderedDict(),
            obj_ids=[], output_dict_per_obj={}, temp_output_dict_per_obj={},
            frames_tracked_per_obj={})

    def _prune(self, state, index):
        cutoff = index - self.memory_frames + 1
        # Keep the initial conditioning output and recent temporal memory.
        # Initial image pixels are unnecessary after their memory is encoded.
        for old in list(state['images']):
            if old != index:
                del state['images'][old]
        for outputs in state['output_dict_per_obj'].values():
            for old in list(outputs['non_cond_frame_outputs']):
                if old < cutoff:
                    del outputs['non_cond_frame_outputs'][old]
        for frames in state['frames_tracked_per_obj'].values():
            for old in list(frames):
                if old < cutoff:
                    del frames[old]

    def track(self, rgb, *, stream_id, object_id, source_ns, box=None, seed_mask=None, reset=False):
        import torch
        if (not isinstance(stream_id, str) or not stream_id or
                not isinstance(object_id, str) or not object_id or
                type(source_ns) is not int or source_ns < 0):
            raise ValueError('sam_source_identity_required')
        rgb = np.asarray(rgb)
        if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError('sam_rgb_contract')
        if box is not None:
            box = np.asarray(box, float)
            if (box.shape != (4,) or not np.isfinite(box).all() or
                    not 0 <= box[0] < box[2] <= rgb.shape[1] or
                    not 0 <= box[1] < box[3] <= rgb.shape[0]):
                raise ValueError('sam_box_invalid')
        if seed_mask is not None:
            seed_mask = np.asarray(seed_mask)
            if seed_mask.dtype != bool or seed_mask.shape != rgb.shape[:2] or not seed_mask.any():
                raise ValueError('sam_seed_mask_invalid')
        key = (stream_id, object_id)
        identity = hashlib.sha256(rgb.tobytes()).hexdigest()
        track = self.tracks.get(key)
        # A repeated source frame is idempotent, not fresh temporal evidence.
        if track is not None and not reset:
            if source_ns < track['source_ns']:
                raise ValueError('sam_source_time_regressed')
            if source_ns == track['source_ns']:
                if identity != track['image_sha256']:
                    raise ValueError('sam_source_pixels_changed')
                return dict(track['result'], duplicate_source=True)
            if tuple(rgb.shape) != track['shape']:
                raise ValueError('sam_stream_dimensions_changed')
        if (track is None or reset) and box is None and seed_mask is None:
            raise ValueError('sam_track_requires_seed')
        autocast = (torch.autocast('cuda', dtype=torch.bfloat16)
                    if self.device.startswith('cuda') else nullcontext())
        # Publish state only after a complete inference. On a failed forward,
        # discard possibly mutated memory rather than using a partial result.
        try:
            with torch.inference_mode(), autocast:
                if track is None or reset:
                    state = self._state(rgb)
                    index = 0
                    if seed_mask is None:
                        self.predictor.add_new_points_or_box(state, frame_idx=0,
                            obj_id=1, box=box)
                    else:
                        self.predictor.add_new_mask(state, frame_idx=0,
                            obj_id=1, mask=seed_mask)
                else:
                    state = track['state']
                    index = state['num_frames']
                    state['images'][index] = self._image(rgb)
                    state['num_frames'] = index + 1
                frames = self.predictor.propagate_in_video(state,
                    start_frame_idx=index, max_frame_num_to_track=0)
                frame_idx, ids, logits = next(frames)
                # Exhaust the generator so upstream cleanup/finalization runs.
                if next(frames, None) is not None or frame_idx != index or list(ids) != [1]:
                    raise RuntimeError('sam_tracking_output_identity')
                output = state['output_dict_per_obj'][0]
                current = output['cond_frame_outputs'].get(index)
                if current is None:
                    current = output['non_cond_frame_outputs'][index]
                presence = float(current['object_score_logits'].sigmoid().item())
                mask = (logits[0, 0] > 0).cpu().numpy()
                result = dict(mask=mask, object_presence_score=presence,
                    score_kind='object_presence_not_mask_iou', source_ns=source_ns,
                    image_sha256=identity, stream_id=stream_id, object_id=object_id,
                    frame_index=index, duplicate_source=False, motion_evidence=False)
                self._prune(state, index)
        except Exception:
            self.tracks.pop(key, None)
            raise
        self.tracks[key] = dict(state=state, source_ns=source_ns,
            image_sha256=identity, shape=tuple(rgb.shape), result=result)
        self.tracks.move_to_end(key)
        while len(self.tracks) > self.max_tracks:
            self.tracks.popitem(last=False)
        return result
