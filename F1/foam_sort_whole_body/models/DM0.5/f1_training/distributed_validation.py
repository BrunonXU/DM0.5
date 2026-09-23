"""Long rank-zero evaluation on a separate CPU group, not a pending NCCL op."""
from datetime import timedelta


def create_validation_group():
    import torch.distributed as dist
    if dist.is_available() and dist.is_initialized():
        # Infrastructure timeout only; does not change optimizer or evaluation samples.
        return dist.new_group(backend='gloo', timeout=timedelta(hours=24))
    return None


def run_rank_zero(function, group):
    import torch.distributed as dist
    distributed = dist.is_available() and dist.is_initialized()
    if distributed and group is None:
        raise ValueError('distributed validation requires a separate CPU process group')
    payload = [None, None]
    if not distributed or dist.get_rank() == 0:
        try:
            payload[1] = function()
        except Exception as exc:
            payload[0] = f'{type(exc).__name__}: {exc}'
    if distributed:
        # No collective is queued on the training NCCL group during long evaluation.
        dist.broadcast_object_list(payload, src=0, group=group)
    if payload[0]:
        raise RuntimeError('F1 validation failed: ' + payload[0])
    return payload[1]
