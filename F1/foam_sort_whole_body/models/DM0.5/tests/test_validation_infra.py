import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from f1_data.reader import sha256
from f1_training.validation_artifacts import write_new, load_report
from f1_training.validation import compare_reports, run_sweep
from f1_training.validation_review import build_review, record_decision
from f1_training.validation_protocol import digest_document
from test_validation_protocol import full_report


def saved_report(directory, name, arm=1):
    path=directory/name; path.mkdir()
    target=np.zeros((50,23)); target[:,[3,7,12,16]]=1
    pred=target.copy(); pred[:,0]+=.001*arm
    pred[:,20]=.1
    np.savez(path/'000000_seed42.npz',physical_prediction=pred,physical_target=target,state19=np.zeros(19))
    report=full_report(arm,1,1)
    report.update(schema='f1_generated_action_metrics_v2',
                  checkpoint_identity={'checkpoint':name,'binding':{'run':'same'},'files':{'adapter_model.safetensors':name}},
                  prediction_files={'000000_seed42.npz':sha256(path/'000000_seed42.npz')},
                  selection={'stage':'pass_b','samples':[{'episode':'a','start':0}],'seeds':[42]},
                  records=[{'episode':'a','start':0,'seed':42,'ik':{'success':True},
                            'metrics':{'20':{'left_position_mm':arm,'right_position_mm':0}}}])
    write_new(path/'report.json',report)
    return path/'report.json'


def test_atomic_report_and_tampering(tmp_path):
    path=saved_report(tmp_path,'checkpoint-a')
    load_report(path)
    with pytest.raises(FileExistsError): write_new(path,{'clobber':True})
    with pytest.raises(ValueError): write_new(tmp_path/'nan.json',{'x':float('nan')})
    assert not (tmp_path/'nan.json').exists()
    (path.parent/'000000_seed42.npz').write_bytes(b'changed')
    with pytest.raises(ValueError,match='prediction file changed'): load_report(path)


def test_compare_review_and_manual_decision(tmp_path):
    a,b=saved_report(tmp_path,'checkpoint-a'),saved_report(tmp_path,'checkpoint-b',2)
    comparison=compare_reports([a,b],repeats=100,seed=42)
    result=tmp_path/'comparison.json'; write_new(result,comparison)
    review=tmp_path/'human.json'
    human={'primary':str(a),'fallback':str(b),'selection_reason':'fixture arms comparison',
           'base_coordination_review':'fixture only, not real acceptance','ik_and_invalid_output_review':'fixture inspected'}
    write_new(review,human)
    record_decision(result,review,tmp_path/'decision.json')
    assert json.loads((tmp_path/'decision.json').read_text())['deployment_performed'] is False
    build_review(a,tmp_path/'plots',limit=1)
    assert (tmp_path/'plots/window_000000.svg').stat().st_size>1000
    assert (tmp_path/'plots/windows.csv').exists()
    human['base_coordination_review']=''; review.write_text(json.dumps(human))
    with pytest.raises(ValueError,match='missing human review'): record_decision(result,review,tmp_path/'bad.json')
    with pytest.raises(ValueError,match='duplicate report paths'): compare_reports([a,a],repeats=10,seed=42)


def test_review_allows_different_missing_motion_subsets(tmp_path):
    path=saved_report(tmp_path,'checkpoint-a')
    report=json.loads(path.read_text())
    second=copy.deepcopy(report['records'][0]); second['seed']=43
    second['metrics']['20']['moving_base_mae']=None
    report['records'].append(second)
    source=path.parent/'000000_seed42.npz'
    dest=path.parent/'000000_seed43.npz'; dest.write_bytes(source.read_bytes())
    report['prediction_files'][dest.name]=sha256(dest)
    path.write_text(json.dumps(report))
    build_review(path,tmp_path/'plots',limit=1)
    assert 'moving_base_mae' in (tmp_path/'plots/windows.csv').read_text()


def test_sweep_retries_partial_attempt_and_reuses_complete(tmp_path,monkeypatch):
    import f1_training.validation as module
    release_path=tmp_path/'release.json'; release_path.write_text('{}')
    selection={'selection':{'stage':'pass_a'},'release_sha256':sha256(release_path)}
    selection_path=tmp_path/'selection.json'; write_new(selection_path,selection)
    checkpoint=tmp_path/'checkpoint-5000'
    identity={'checkpoint':str(checkpoint),'binding':{'run':'same'},'files':{'weights':'frozen'}}
    monkeypatch.setattr(module,'Release',lambda *a,**k:SimpleNamespace(path=release_path))
    monkeypatch.setattr(module,'validate_checkpoint',lambda *a:identity)
    calls=[]
    def execute(command,**kwargs):
        calls.append(command)
        dest=Path(command[command.index('--output')+1])
        if len(calls)==1:
            raise RuntimeError('fixture early crash before mkdir')
        path=saved_report(dest.parent,dest.name)
        report=json.loads(path.read_text()); report['checkpoint_identity']=identity
        report['comparison_contract']['selection_sha256']=digest_document(selection['selection'])
        path.write_text(json.dumps(report))
    monkeypatch.setattr(module.subprocess,'run',execute)
    args=SimpleNamespace(release=str(release_path),engineering=True,selection=str(selection_path),
                         output=str(tmp_path/'sweep'),checkpoints=[str(checkpoint)],device='cpu')
    with pytest.raises(RuntimeError,match='early crash'): run_sweep(args)
    run_sweep(args); run_sweep(args)
    assert len(calls)==2
    assert (tmp_path/'sweep/000_checkpoint-5000/attempt-002/report.json').exists()
