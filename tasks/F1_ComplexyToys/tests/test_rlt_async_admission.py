"""Worker contracts without Ray launch, gradients, or robot IO."""
import json,os,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
torch=pytest.importorskip('torch')
pytest.importorskip('ray')
from omegaconf import OmegaConf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,os.environ.get('F1_RLT_CORE',str(Path(__file__).resolve().parents[3]/'RLinf')))
from f1_toys import rlt_async_workers as workers
from test_rlt_critic_calibration import data


def test_retraction_catches_expert_removal_and_changed_observation():
    from f1_toys.rlt_transition_ledger import retracted_ids
    row=dict(episode_id='e',decision_ns=1,next_decision_ns=2,command_sequences=[1])
    old={'action_time_mask':torch.tensor([[True]]),'expert_approved':torch.tensor([[False]]),
         'obs':{'z_rl':torch.tensor([[1.]])},'reward':torch.tensor([0.])}
    import copy
    added=copy.deepcopy(old);added['expert_approved'][0,0]=True
    assert not retracted_ids([row],old,[row],added)
    assert retracted_ids([row],added,[row],old)
    changed=copy.deepcopy(old);changed['obs']['z_rl'][0,0]=2
    assert retracted_ids([row],old,[row],changed)


def test_promotion_rechecks_current_critic_even_when_actions_pass(monkeypatch):
    monkeypatch.setattr(workers,'evaluate_candidate',lambda *a:dict(checks={'actions':True},passed=True))
    learner=SimpleNamespace(cfg={'enable_q_guidance':True},
        model=SimpleNamespace(_critic_state=lambda x:x['state']),
        critic=lambda *a:torch.tensor([[.9,.8],[.3,.4]]))
    worker=SimpleNamespace(learner=learner,validation={'holdout':('unused','digest',data([1,0]))},
        replay={},critic_anchors=None,baseline=None,incumbent=None,
        cfg=OmegaConf.create({'f1':{'kinematic_gate':False}}))
    result=workers.F1ActorWorker._evaluate(worker)
    assert result['checks']['actions'] and not result['checks']['current_critic_calibrated']
    assert not result['passed']
    learner.critic=lambda *a:torch.tensor([[.9,.8],[.2,.1]])
    assert workers.F1ActorWorker._evaluate(worker)['passed']


def test_restore_credits_new_seed_once_without_replaying_receipt(tmp_path):
    (tmp_path/'replay-catalog.json').write_text('[]')
    (tmp_path/'data-budget.json').write_text(json.dumps(dict(received_transitions=4,credited_episodes=['old'])))
    worker=SimpleNamespace(learner=SimpleNamespace(restore=lambda *a:None),identity={},
        replay={'old':('old','hash',data([0]*4)),'new':('new','hash',data([0]*3))},
        received_transitions=7,credited_episodes={'old','new'},credited_transitions=set())
    def ingest(episode):
        count = 4 if episode == 'old' else 3
        if episode not in worker.credited_episodes:
            worker.received_transitions += count
            worker.credited_episodes.add(episode)
        worker.replay[episode] = (episode,'hash',data([0]*count))
    worker._ingest = ingest
    workers.F1ActorWorker.load_checkpoint(worker,tmp_path)
    assert worker.received_transitions==7 and worker.credited_episodes=={'old','new'}
    workers.F1ActorWorker.load_checkpoint(worker,tmp_path)
    assert worker.received_transitions==7
