"""Optional semantic seed provider; tracking, geometry and authority stay external."""
import hashlib
from pathlib import Path
import sys

import numpy as np


SEMANTIC_CANDIDATE_SUMMARY_LIMIT = 64


def unique_scoped_candidate(masks, boxes, box):
    """Use the explicit current-frame search scope, never a score argmax."""
    centers = (boxes[:, :2] + boxes[:, 2:]) / 2
    inside = ((centers >= np.asarray(box[:2])).all(axis=1) &
              (centers <= np.asarray(box[2:])).all(axis=1))
    indices = [int(i) for i in np.flatnonzero(inside) if masks[i].any()]
    return indices[0] if len(indices) == 1 else None, indices


class Sam3SeedProvider:
    def __init__(self, config, device):
        checkpoint = Path(config['checkpoint'])
        digest = hashlib.sha256()
        with checkpoint.open('rb') as file:
            for block in iter(lambda: file.read(8 * 1024**2), b''):
                digest.update(block)
        self.identity = digest.hexdigest()
        if self.identity != config['checkpoint_sha256']:
            raise ValueError('sam_seed_checkpoint_identity_changed')
        sys.path[:0] = config['python_paths']
        from sam3.model_builder import build_sam3_image_model
        from sam3.model.sam3_image_processor import Sam3Processor
        self.model = build_sam3_image_model(checkpoint_path=str(checkpoint),
            load_from_HF=False, device=device, compile=False, enable_inst_interactivity=False)
        # Pinned upstream only special-cases the literal 'cuda', not 'cuda:0'.
        self.model.to(device).eval()
        self.processor = Sam3Processor(self.model, device=device, resolution=1008,
            confidence_threshold=config['candidate_threshold'])
        self.prompts = dict(config['object_prompts'])
        self.device = device

    def seed(self, rgb, *, object_id, box, points=None, labels=None):
        if object_id not in self.prompts or points is not None:
            return None, dict(seed_backend='sam2', seed_reason='explicit_points_or_unconfigured_entity')
        import torch
        from PIL import Image
        with torch.inference_mode(), torch.autocast('cuda', dtype=torch.bfloat16):
            state = self.processor.set_image(Image.fromarray(rgb))
            state = self.processor.set_text_prompt(self.prompts[object_id], state)
            masks = state['masks'].detach().cpu().numpy().astype(bool).reshape(-1, *rgb.shape[:2])
            boxes = state['boxes'].detach().float().cpu().numpy().reshape(-1, 4)
            scores = state['scores'].detach().float().cpu().numpy().reshape(-1)
        index, scoped = unique_scoped_candidate(masks, boxes, box)
        # No semantic match is an ordinary observation miss, not permission to
        # seed SAM2 on whatever occupies the old box (often the robot's hand).
        order = sorted(range(len(masks)), key=lambda i: float(scores[i]), reverse=True)
        truncated = len(masks) > SEMANTIC_CANDIDATE_SUMMARY_LIMIT
        summary = [
            dict(
                index=int(i),
                box=[int(round(v)) for v in boxes[i].tolist()],
                area_px=int(masks[i].sum()),
                score=round(float(scores[i]), 3),
                in_scope=i in scoped,
            )
            for i in order[:SEMANTIC_CANDIDATE_SUMMARY_LIMIT]
        ]
        detail = dict(seed_backend='sam3',
            seed_reason='unique_in_current_hint' if index is not None else 'semantic_scope_ambiguous_or_empty',
            identity_status='confirmed' if index is not None else 'unresolved',
            identity_source='semantic',
            semantic_model_sha256=self.identity, semantic_candidates=len(masks),
            semantic_scoped_candidates=len(scoped), semantic_selected_index=index,
            semantic_candidate_summary=summary,
            semantic_candidate_summary_truncated=truncated)
        return ((masks[index], float(scores[index])) if index is not None else None), detail
