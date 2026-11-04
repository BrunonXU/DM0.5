"""Versioned exploration contract, separate from frozen VLA reference RNG."""
import math
import secrets


def sampling_contract(head):
    value = head.get('actor_sampling')
    if value is None:
        return None  # Historical identities and deterministic outputs stay exact.
    if (set(value) != {'schema', 'mode', 'fixed_std'}
            or value['schema'] != 'f1_rlt_sampling_v1'
            or value['mode'] not in ('deterministic', 'fixed_gaussian')
            or not isinstance(value['fixed_std'], (float, int))
            or not math.isfinite(value['fixed_std']) or value['fixed_std'] <= 0
            or value['fixed_std'] != head['model_config']['fixed_std']):
        raise ValueError('Invalid actor exploration contract')
    return dict(value)


def sample_actor(actor, features, contract, *, seed=None):
    import torch
    deterministic = contract is None or contract['mode'] == 'deterministic'
    if deterministic:
        return actor.sac_forward(features, deterministic=True)[0], None
    if contract['fixed_std'] != actor.fixed_std:
        raise ValueError('Actor noise scale differs from versioned contract')
    seed = secrets.randbits(63) if seed is None else seed
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError('Invalid actor exploration seed')
    device = next(actor.parameters()).device
    devices = [device.index] if device.type == 'cuda' else []
    # Restore global RNG afterward; actor exploration cannot change the VLA RNG.
    with torch.random.fork_rng(devices=devices):
        torch.random.default_generator.manual_seed(seed)
        if devices:
            with torch.cuda.device(device):
                torch.cuda.manual_seed(seed)
        action = actor.sac_forward(features, deterministic=False)[0]
    return action, seed
