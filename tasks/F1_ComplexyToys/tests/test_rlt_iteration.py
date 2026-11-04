from pathlib import Path
import hashlib,json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_data_policy import load_policy,episode_selection


def test_episode_selection_is_source_bound_and_fail_closed(tmp_path):
    source=tmp_path/'receipt.json';source.write_text(json.dumps({'episode_id':'good'}))
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    value={'schema':'f1_rlt_data_policy_v1','default':'quarantine','episodes':{
        'good':{'split':'train','source_receipt_sha256':digest,'reason':'reviewed'},
        'heldout':{'split':'validation','source_receipt_sha256':'a'*64,'reason':'heldout'}}}
    path=tmp_path/'policy.json';path.write_text(json.dumps(value));policy=load_policy(path)
    assert episode_selection(policy,source)=='train'
    source.write_text(json.dumps({'episode_id':'unknown'}));assert episode_selection(policy,source)=='quarantine'
    source.write_text(json.dumps({'episode_id':'good','changed':True}))
    with pytest.raises(ValueError,match='changed'):episode_selection(policy,source)
    with pytest.raises(ValueError,match='changed'):load_policy(path,'f'*64)


def test_conditioning_export_and_gradient_equivalence():
    torch=pytest.importorskip('torch')
    from f1_toys.rlt_conditioning import install_conditioning,export_actor_state
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__();self.backbone=torch.nn.Sequential(torch.nn.Linear(3,4),torch.nn.Tanh());self.actor_mean=torch.nn.Linear(4,2)
        def forward(self,x):return self.actor_mean(self.backbone(x))
    torch.manual_seed(4);model=Model();x=torch.randn(10,3)*3;before=model(x).detach()
    install_conditioning(model,{'mean':[1,2,-1],'scale':[2,3,.4]})
    torch.testing.assert_close(model(x),before,atol=1e-6,rtol=1e-6)
    model(x).square().mean().backward()
    with torch.no_grad():
        for p in model.parameters():p.add_(p.grad,alpha=-.01)
    exported=Model();exported.load_state_dict(export_actor_state(model))
    torch.testing.assert_close(model(x),exported(x),atol=1e-6,rtol=1e-6)


def test_gate_rejects_more_early_release_even_with_lower_loss():
    pytest.importorskip('torch')
    from f1_toys.rlt_evaluation import promotion_checks
    base=dict(finite=True,human_mse=1.,xyz_rmse=.1,gripper_mae=1.,release_recall=0.,premature_release=0.,release_steps=3,hold_steps=20)
    candidate=dict(base,human_mse=.1,gripper_mae=.1,release_recall=1.,premature_release=.1)
    assert not all(promotion_checks(candidate,base).values())
    candidate['premature_release']=0.
    assert all(promotion_checks(candidate,base).values())


@pytest.mark.parametrize('protocol', ['f1_rlt_local_v2', 'f1_rlt_local_v3', 'f1_rlt_local_v4'])
def test_future_episode_split_is_deterministic_and_identity_bound(protocol):
    from f1_toys.rlt_data_policy import select_receipt
    policy={'episodes':{},'future':{'not_before_utc':'20261028T040000Z','base_model_version':'base',
        'encoder_manifest_sha256':'encoder','validation_modulus':5}}
    row={'episode_id':'f1_hil_20261028T050000Z_abcdef','schema':'f1_local_journal_prefix_v1',
         'binding':{'recording_boundary':protocol,'execution_mode':'powered','robot':'F1-09',
                    'base_model_version':'base','rlt':{'encoder_manifest_sha256':'encoder'}}}
    first=select_receipt(policy,row,'a'*64);assert first in ('train','validation')
    row['local_result']={'status':'failure'};assert select_receipt(policy,row,'b'*64)==first
    row['binding']['base_model_version']='different';assert select_receipt(policy,row,'a'*64)=='quarantine'


def test_stationary_feedback_with_commands_is_quarantined():
    from test_rlt_local_replay import fixture
    from f1_toys.rlt_local_replay import build_transitions
    streams,result=fixture()
    streams['control'][6]['submitted'][7]=5.
    with pytest.raises(ValueError,match='stationary'):build_transitions(streams,result)


def test_gate_cannot_accumulate_regression_against_weak_incumbent():
    pytest.importorskip('torch')
    from f1_toys.rlt_evaluation import promotion_checks
    base=dict(finite=True,human_mse=1.,xyz_rmse=.1,gripper_mae=1.,release_recall=.5,premature_release=0.,release_steps=3,hold_steps=20)
    incumbent=dict(base,human_mse=2.,xyz_rmse=.2,gripper_mae=.8)
    candidate=dict(base,human_mse=1.8,xyz_rmse=.18,gripper_mae=.7,release_recall=.6)
    checks=promotion_checks(candidate,base,incumbent)
    assert not checks['human_fit'] and not checks['translation_nonregression']


def test_kinematic_screen_accepts_source_decision_without_fake_submitted_chunk(monkeypatch):
    torch=pytest.importorskip('torch')
    from f1_toys.rlt_evaluation import kinematic_screen
    import f1_toys.eef as eef
    class Actor:
        def sac_forward(self,obs,deterministic):return (torch.zeros(1,160),)
    calls=[]
    def decode(ik,physical,state,held,execute_steps):
        calls.append((state.tolist(),held.tolist()));return {'success':True}
    monkeypatch.setattr(eef,'solver',lambda:object())
    monkeypatch.setattr(eef,'physical_actions',lambda raw,stats:raw)
    monkeypatch.setattr(eef,'decode_prefix',decode)
    data={'actions':torch.zeros(1,16,10),'obs':{},'terminated':torch.tensor([True])}
    report=kinematic_screen(Actor(),Actor(),data,[dict(observation_id=1,state16=[1.]*16,decision_kind='actor_request')],{})
    assert report['passed'] and calls==[([1.]*16,[1.]*16)]*2
