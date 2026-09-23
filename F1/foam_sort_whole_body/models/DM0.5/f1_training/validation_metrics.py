"""F1 fieldwise counterparts of G2 horizon/dynamic/seed diagnostics."""
import numpy as np
from f1_data.reader import decode_rotation6d

HORIZONS = (1, 5, 10, 20, 25, 50)


def finite_mean(values, *, root=False):
    values = np.asarray(values)
    if not values.size or not np.isfinite(values).all():
        return None
    mean = float(values.mean())
    return float(np.sqrt(mean)) if root else mean


def rotation_errors(actual, target):
    values = []
    for a, b in zip(actual, target):
        try:
            r, t = decode_rotation6d(a), decode_rotation6d(b)
            values.append(float(np.degrees(np.arccos(np.clip((np.trace(r @ t.T)-1)/2, -1, 1)))))
        except ValueError:
            values.append(np.nan)
    return np.asarray(values)


def extra_metrics(prediction, truth, horizon, action_mask=None):
    p, t = prediction[:horizon], truth[:horizon]
    mask = np.ones_like(t, dtype=bool) if action_mask is None else np.asarray(action_mask, dtype=bool)[:horizon, :23]
    result = {}
    for side, offset in (('left', 0), ('right', 9)):
        distances = np.linalg.norm(p[:, offset:offset+3] - t[:, offset:offset+3], axis=-1)*1000
        angles = rotation_errors(p[:, offset+3:offset+9], t[:, offset+3:offset+9])
        result[side + '_position_rmse_mm'] = finite_mean(distances**2, root=True)
        # Include invalid count separately; do not conceal it in a finite-only score.
        result[side + '_rotation_rmse_deg_valid_only'] = finite_mean(angles[np.isfinite(angles)]**2, root=True)
        for step in HORIZONS:
            if step <= horizon:
                result[f'{side}_position_at_h{step}_mm'] = float(distances[step-1]) if np.isfinite(distances[step-1]) else None
                result[f'{side}_rotation_at_h{step}_deg'] = float(angles[step-1]) if np.isfinite(angles[step-1]) else None
        for order, label in ((1, 'velocity'), (2, 'acceleration'), (3, 'jerk')):
            delta = (np.diff(p[:, offset:offset+3], n=order, axis=0) - np.diff(t[:, offset:offset+3], n=order, axis=0))*30**order
            result[f'{side}_translation_{label}_rmse_m_s{order}'] = finite_mean(delta**2, root=True)
    for dim, label in ((18, 'left_gripper'), (19, 'right_gripper'), (20, 'vx'), (21, 'vy'), (22, 'wz')):
        valid = mask[:, dim]
        result[label + '_rmse'] = finite_mean((p[valid,dim]-t[valid,dim])**2, root=True)
        for step in HORIZONS:
            if step <= horizon:
                value = abs(p[step-1,dim]-t[step-1,dim])
                result[f'{label}_at_h{step}_absolute_error'] = float(value) if valid[step-1] and np.isfinite(value) else None
    # Base action is already VELOCITY: its first difference is acceleration.
    for dim, label in ((20, 'vx'), (21, 'vy'), (22, 'wz')):
        for order, name in ((1, 'acceleration'), (2, 'jerk')):
            delta = (np.diff(p[:,dim], n=order)-np.diff(t[:,dim], n=order))*30**order
            valid = np.lib.stride_tricks.sliding_window_view(mask[:, dim], order+1).all(axis=1)
            result[f'base_{label}_{name}_rmse'] = finite_mean(delta[valid]**2, root=True)
    result['gripper_out_of_0_100_elements'] = int(((p[:,18:20]<0)|(p[:,18:20]>100)).sum())
    return result


def seed_dispersion(predictions):
    """As in NewLoRA: differences to the first fixed generation seed, by field."""
    if len(predictions) < 2:
        return {}
    reference = predictions[0]
    lists = {}
    def add(key, value):
        lists.setdefault(key, []).append(value)
    for p in predictions[1:]:
        for horizon in (20,50):
            for side, offset in (('left',0),('right',9)):
                distance = np.linalg.norm(p[:horizon,offset:offset+3]-reference[:horizon,offset:offset+3], axis=-1)*1000
                add(f'h{horizon}/{side}_position_mm', finite_mean(distance))
                angle = rotation_errors(p[:horizon,offset+3:offset+9], reference[:horizon,offset+3:offset+9])
                add(f'h{horizon}/{side}_rotation_deg_valid_only', finite_mean(angle[np.isfinite(angle)]))
                add(f'h{horizon}/{side}_rotation_invalid_pairs', int((~np.isfinite(angle)).sum()))
            for dim, label in ((18,'left_gripper'),(19,'right_gripper'),(20,'vx'),(21,'vy'),(22,'wz')):
                add(f'h{horizon}/{label}_mae', finite_mean(np.abs(p[:horizon,dim]-reference[:horizon,dim])))
    return {key: finite_mean(values) if all(v is not None for v in values) else None for key,values in lists.items()}
