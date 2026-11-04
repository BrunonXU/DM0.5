"""Known actions are context, not new decisions of the small RLT actor."""
import torch


def with_prefix(actions, obs):
    prefix = obs.get('committed_actions')
    mask = obs.get('committed_mask')
    if prefix is None and mask is None:
        return actions
    shaped = actions.reshape(len(actions), 16, 10)
    if (prefix is None or mask is None or prefix.shape != shaped.shape
            or mask.shape != shaped.shape[:2] or mask.dtype != torch.bool
            or (mask[:, 1:] & ~mask[:, :-1]).any() or mask.all(1).any()
            or not torch.isfinite(prefix).all()):
        raise ValueError('Invalid committed RLT prefix')
    return torch.where(mask[..., None], prefix.detach(), shaped).reshape_as(actions)


def add_empty_prefix(obs):
    """Upgrade old immutable tensors in memory, without changing source files."""
    if 'committed_mask' in obs or 'committed_actions' in obs:
        with_prefix(obs['ref_chunk'][:, :16].reshape(len(obs['ref_chunk']), -1), obs)
        return dict(obs, decision_steps=obs.get('decision_steps', torch.full(
            (len(obs['ref_chunk']),),16,dtype=torch.long,device=obs['ref_chunk'].device)))
    ref = obs['ref_chunk'][:, :16]
    return dict(obs, committed_actions=torch.zeros_like(ref),
                committed_mask=torch.zeros(ref.shape[:2], dtype=torch.bool, device=ref.device),
                decision_steps=torch.full((len(ref),),16,dtype=torch.long,device=ref.device))


def planned_mask(obs, template):
    """Query the intended decision horizon, not an unexecuted full RTC chunk."""
    steps = obs.get('decision_steps')
    if steps is None:
        return torch.ones_like(template)
    if (steps.shape != (len(template),) or (steps != steps.long()).any()
            or (steps < 1).any() or (steps > template.shape[1]).any()):
        raise ValueError('Invalid planned RLT decision horizon')
    return torch.arange(template.shape[1],device=template.device)[None] < steps[:,None]
