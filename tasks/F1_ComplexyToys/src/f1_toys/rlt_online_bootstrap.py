"""Explicit new-run warm start: deployed actor, existing corrected critic."""
from pathlib import Path

import torch

from .rlt_online import sha


def warm_start(learner, spec, feature_identity, initial_head_sha256):
    from .rlt_action_contract import SUBMITTED
    if learner.cfg.get('action_representation', SUBMITTED) != spec.get('parent_action_representation', SUBMITTED):
        raise ValueError('Cannot warm-start critic across controller action representations')
    root = Path(spec['path'])
    if spec.get('schema') == 'f1_rlt_offline_bootstrap_v1':
        path=root/'learner.pt'
        if sha(path) != spec['files_sha256']['learner.pt']:
            raise ValueError('Offline bootstrap checkpoint changed')
        saved=torch.load(path,map_location=learner.device,weights_only=True)
        identity=saved['identity']
        if (identity['data']['initial_actor_manifest_sha256'] != initial_head_sha256
                or identity['recipe'] != learner.cfg or saved['step'] != spec['parent_step']
                or identity['data']['migration_sha256'] != spec['migration_sha256']):
            raise ValueError('Offline bootstrap recipe/data/ancestry mismatch')
        for name in ('model','critic','target'):
            if any(not torch.isfinite(v).all() for v in saved[name].values()):
                raise ValueError('Nonfinite offline bootstrap weights')
        # Preserve optimizer and Q counters; do not relabel a trained Q actor
        # as a new BC model or replay historical update credit a second time.
        learner.restore(root,identity)
        return
    for name in ('learner.pt', 'incumbent.pt'):
        if sha(root/name) != spec['files_sha256'][name]:
            raise ValueError('Bootstrap checkpoint changed')
    saved = torch.load(root/'learner.pt', map_location=learner.device, weights_only=True)
    if (saved['identity']['feature_identity'] != feature_identity
            or saved['identity']['initial_head_sha256'] != initial_head_sha256
            or saved['step'] != spec['parent_step']):
        raise ValueError('Bootstrap feature/actor ancestry mismatch')
    actor = torch.load(root/'incumbent.pt', map_location=learner.device, weights_only=True)
    state = learner.model.state_dict()
    expected = {k for k in state if k.startswith(('backbone.', 'actor_mean.'))}
    if set(actor) != expected or any(not torch.isfinite(v).all() for v in actor.values()):
        raise ValueError('Bootstrap incumbent actor invalid')
    state.update(actor)
    learner.model.load_state_dict(state, strict=True)
    learner.critic.load_state_dict(saved['critic'], strict=True)
    learner.target.load_state_dict(saved['target'], strict=True)
    learner.critic_optim.load_state_dict(saved['critic_optimizer'])
    if any(not torch.isfinite(v).all() for m in (learner.critic, learner.target) for v in m.state_dict().values()):
        raise ValueError('Bootstrap critic invalid')
    # New objective/sampling run: actor optimizer and RNG start with its seed.
    # Parent remains immutable and is not relabelled as a resumed run.
    learner.step = learner.q_updates = 0
