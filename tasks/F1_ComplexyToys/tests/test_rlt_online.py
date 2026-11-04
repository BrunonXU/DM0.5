"""CPU numerical adapter checks; synthetic tensors, no robot/network output."""
import copy
from pathlib import Path
import sys

import pytest
torch = pytest.importorskip('torch')
pytest.importorskip('safetensors')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_online import TemporalCritic
from f1_toys.rlt_stage2 import make_policy


def test_reference_tail_regularization_keeps_real_human_prefix_weight():
    from f1_toys.rlt_online import actor_bc_loss
    pi = torch.ones(1, 16, 10, requires_grad=True)
    labels = torch.zeros_like(pi)
    mask = torch.zeros(1, 16, dtype=torch.bool); mask[:, :3] = True
    old, _, _ = actor_bc_loss(pi, labels, mask)
    old_grad = torch.autograd.grad(old, pi, retain_graph=True)[0]
    repaired, _, _ = actor_bc_loss(pi, labels, mask, 'prefix_plus_reference_tail_v2')
    grad = torch.autograd.grad(repaired, pi)[0]
    torch.testing.assert_close(grad[:, :3], old_grad[:, :3])
    assert old_grad[:, 3:].abs().sum() == 0 and grad[:, 3:].abs().sum() > 0


def test_shared_sampler_is_reproducible_and_does_not_mutate_sources():
    from f1_toys.rlt_training_batch import sample_training_batch
    def data(ids, human):
        return dict(actions=torch.tensor(ids)[:, None, None].float(),
                    intervene_flags=torch.tensor(human)[:, None],
                    action_time_mask=torch.ones(len(ids), 1, dtype=torch.bool))
    local, demos = data([1, 2, 3], [True, False, False]), data([100], [True])
    local['expert_approved'] = local['intervene_flags'].clone()
    demos['expert_approved'] = demos['intervene_flags'].clone()
    a, stats = sample_training_batch(local, demos, 100, .5, generator=torch.Generator().manual_seed(1))
    b, _ = sample_training_batch(local, demos, 100, .5, generator=torch.Generator().manual_seed(1))
    torch.testing.assert_close(a['actions'], b['actions'])
    assert stats['expert_size'] == 2 and stats['expert_sample_fraction'] == .5
    assert set(a['actions'][:50].flatten().tolist()) == {1., 100.}
    assert set(a['actions'][50:].flatten().tolist()) <= {1., 2., 3.}
    assert local['actions'].flatten().tolist() == [1., 2., 3.]


def test_rtc_prefix_is_fixed_context_with_no_actor_gradient():
    from f1_toys.rlt_prefix import with_prefix, add_empty_prefix
    pi = torch.randn(2, 160, requires_grad=True)
    obs = add_empty_prefix(dict(ref_chunk=torch.randn(2,50,10)))
    obs['committed_mask'][:, :6] = True
    obs['committed_actions'][:, :6] = .25
    out = with_prefix(pi, obs).reshape(2,16,10)
    assert (out[:,:6] == .25).all()
    out.sum().backward()
    assert pi.grad.reshape(2,16,10)[:,:6].count_nonzero() == 0
    assert (pi.grad.reshape(2,16,10)[:,6:] == 1).all()
    obs['committed_mask'][:,8] = True
    with pytest.raises(ValueError,match='committed'):
        with_prefix(pi,obs)


def test_mixed_replay_uses_rtc_decision_horizon_without_rewriting_sync_data():
    from f1_toys.rlt_prefix import add_empty_prefix, planned_mask
    from f1_toys.rlt_critic_calibration import concatenate
    sync=dict(ref_chunk=torch.zeros(1,50,10))
    rtc=add_empty_prefix(sync);rtc['decision_steps'].fill_(8)
    a=dict(obs=sync,next_obs=sync,actions=torch.zeros(1,16,10))
    b=dict(obs=rtc,next_obs=rtc,actions=torch.ones(1,16,10))
    merged=concatenate([a,b])
    mask=planned_mask(merged['obs'],torch.zeros(2,16,dtype=torch.bool))
    assert mask.sum(-1).tolist()==[16,8]
    assert set(sync)=={'ref_chunk'}
    merged['obs']['decision_steps'][1]=17
    with pytest.raises(ValueError,match='decision horizon'):
        planned_mask(merged['obs'],mask)


def test_partial_padding_does_not_change_q_and_duration_is_explicit():
    torch.set_num_threads(2)
    task = Path(__file__).resolve().parents[1]
    import os
    model = make_policy(os.environ.get('F1_RLT_CORE', str(task.parents[1]/'RLinf')),
        dict(z_dim=2048, proprio_dim=8, action_dim=10, chunk_len=16, ref_chunk_len=50, fixed_std=.002))
    critic = TemporalCritic(model.q_head, 16)
    state = torch.zeros(2, 2056)
    action = torch.randn(2, 16, 10)
    mask = torch.zeros(2, 16, dtype=torch.bool); mask[:, :3] = True
    elapsed = torch.tensor([2.3, 2.8])
    first = critic(state, action, mask, elapsed)
    changed = action.clone(); changed[:, 3:] = 9000
    torch.testing.assert_close(first, critic(state, changed, mask, elapsed), rtol=0, atol=0)
    first.sum().backward()
    assert any(p.grad is not None for p in critic.parameters())
    mask[:, 5] = True
    with pytest.raises(ValueError, match='contiguous'): critic(state, action, mask, elapsed)


def test_reference_critic_does_not_need_future_execution_duration():
    import os
    task=Path(__file__).resolve().parents[1]
    model=make_policy(os.environ.get('F1_RLT_CORE',str(task.parents[1]/'RLinf')),
        dict(z_dim=2048,proprio_dim=8,action_dim=10,chunk_len=16,ref_chunk_len=50,fixed_std=.002))
    critic=TemporalCritic(model.q_head,16,condition_on_elapsed=False)
    # Even with a learned nonzero last column, future duration cannot change Q.
    with torch.no_grad():
        for head in critic.q_head.qs:head.net[0].weight[:,-1].fill_(1.)
    state=torch.zeros(2,2056);a=torch.randn(2,16,10);mask=torch.ones(2,16,dtype=torch.bool)
    torch.testing.assert_close(critic(state,a,mask,torch.tensor([16.,16.])),
                               critic(state,a,mask,torch.tensor([35.,42.])),rtol=0,atol=0)


def test_partial_waypoint_fraction_is_validated_and_visible_to_critic():
    import os
    task=Path(__file__).resolve().parents[1]
    model=make_policy(os.environ.get('F1_RLT_CORE',str(task.parents[1]/'RLinf')),
        dict(z_dim=2048,proprio_dim=8,action_dim=10,chunk_len=16,ref_chunk_len=50,fixed_std=.002))
    critic=TemporalCritic(model.q_head,16,condition_on_elapsed=False)
    state=torch.zeros(1,2056);a=torch.zeros(1,16,10)
    mask=torch.zeros(1,16,dtype=torch.bool);mask[:,:3]=True
    fractions=mask.float();fractions[:,2]=.4
    seen=[]
    hook=critic.q_head.qs[0].net[0].register_forward_pre_hook(lambda m,args:seen.append(args[0].detach()))
    critic(state,a,mask,torch.tensor([18.]),fractions)
    hook.remove()
    torch.testing.assert_close(seen[0][:,-17:-1],fractions)
    fractions[:,4]=.2
    with pytest.raises(ValueError,match='fractions'):critic(state,a,mask,torch.tensor([18.]),fractions)


def test_projected_critic_evaluates_served_rotation_and_keeps_gradients():
    import os
    task=Path(__file__).resolve().parents[1]
    model=make_policy(os.environ.get('F1_RLT_CORE',str(task.parents[1]/'RLinf')),
        dict(z_dim=2048,proprio_dim=8,action_dim=10,chunk_len=16,ref_chunk_len=50,fixed_std=.002))
    critic=TemporalCritic(model.q_head,16,condition_on_elapsed=False,
                         rotation_projection='serving_gram_schmidt_v1')
    state=torch.randn(2,2056)
    action=torch.zeros(2,16,10);action[...,3]=1;action[...,7]=1
    equivalent=action.clone();equivalent[...,3]=2;equivalent[...,6]=.4;equivalent[...,7]=3
    mask=torch.ones(2,16,dtype=torch.bool);elapsed=torch.tensor([16.,20.])
    torch.testing.assert_close(critic(state,action,mask,elapsed),
                               critic(state,equivalent,mask,elapsed),rtol=0,atol=0)
    equivalent.requires_grad_();critic(state,equivalent,mask,elapsed).sum().backward()
    assert torch.isfinite(equivalent.grad).all() and equivalent.grad[...,3:9].abs().sum()>0
    bad=action.clone();bad[...,3:9]=0
    with pytest.raises(ValueError,match='degenerate'):critic(state,bad,mask,elapsed)
    mask[:,8:]=False;action[:,8:]=0
    assert torch.isfinite(critic(state,action,mask,elapsed)).all()
