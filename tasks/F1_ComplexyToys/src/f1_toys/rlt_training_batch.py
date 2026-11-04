"""Shared immutable replay sampling for offline trials and the async worker."""
import torch

from .rlt_critic_calibration import concatenate, subset


SAMPLER = 'reviewed_expert_and_local_v2'


def sample_training_batch(local, demos, batch_size, expert_fraction=.5, *, generator=None):
    if batch_size < 1 or not 0 <= expert_fraction <= 1 or not len(local['actions']):
        raise ValueError('Nonempty replay and valid sampling proportions required')
    from .rlt_supervision import expert_mask
    local = dict(local, expert_approved=expert_mask(local))
    human = torch.nonzero(local['expert_approved'].any(1)).flatten()
    expert = subset(local, human)
    expert_origins = human.tolist()
    if demos is not None:
        demos = dict(demos, expert_approved=expert_mask(demos))
        demos = subset(demos, torch.nonzero(demos['expert_approved'].any(1)).flatten())
        expert = concatenate([demos, expert])
        expert_origins = [None]*len(demos['actions'])+expert_origins
    count = int(batch_size*expert_fraction) if len(expert['actions']) else 0
    consumed = []
    def draw(data, n, origins=None):
        indices = torch.randint(len(data['actions']), (n,), generator=generator)
        consumed.extend(i if origins is None else origins[i] for i in indices.tolist())
        return subset(data, indices)
    parts = []
    if count:
        parts.append(draw(expert, count, expert_origins))
    if count < batch_size:
        parts.append(draw(local, batch_size-count))
    return concatenate(parts), dict(sampler=SAMPLER, expert_size=len(expert['actions']),
                                    sampled_local_indices=sorted({i for i in consumed if i is not None}),
                                    expert_sample_fraction=count/batch_size)
