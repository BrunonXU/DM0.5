"""Independent expert approval; human execution alone is not supervision."""
CONTRACT = 'reviewed_expert_steps_v1'


def expert_mask(data):
    import torch
    mask = data.get('expert_approved', torch.zeros_like(data['action_time_mask']))
    if (mask.dtype != torch.bool or mask.shape != data['action_time_mask'].shape
            or (mask & ~(data.get('intervene_flags', torch.zeros_like(mask)) & data['action_time_mask'])).any()):
        raise ValueError('Expert approval must be a subset of executed human steps')
    return mask


def approved_steps(record, review):
    """A bound review approves intervals; no approval inferred from outcome."""
    ranges = [] if review is None else review.get('expert_intervals', [])
    if ranges and not str(review.get('reviewer', '')).strip():
        raise ValueError('Expert approval requires a reviewer')
    for span in ranges:
        if (set(span) != {'start_ns', 'end_ns'} or type(span['start_ns']) is not int
                or type(span['end_ns']) is not int or span['start_ns'] >= span['end_ns']):
            raise ValueError('Invalid expert interval')
    grid = record.get('action_grid_ns')
    def approved(i):
        start = record['decision_ns'] if grid is None else (record['decision_ns'] if i == 0 else grid[i-1])
        end = record['next_decision_ns'] if grid is None else grid[i]
        return any(span['start_ns'] <= start and end <= span['end_ns'] for span in ranges)
    return [bool(valid and human and approved(i)) for i, (valid, human) in
            enumerate(zip(record['action_time_mask'], record['intervene_flags']))]
