"""A restored offline learner and official runner share one update counter."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
pytest.importorskip('ray')
from omegaconf import OmegaConf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,os.environ.get('F1_RLT_CORE',str(Path(__file__).resolve().parents[3]/'RLinf')))
from f1_toys.rlt_runner import F1AsyncRunner,AsyncEmbodiedRunner


def test_offline_bootstrap_aligns_runner_without_restarting_steps(monkeypatch):
    monkeypatch.setattr(AsyncEmbodiedRunner,'init_workers',lambda self:None)
    state={'step':1024}
    obj=object.__new__(F1AsyncRunner)
    obj.cfg=OmegaConf.create(dict(runner={'resume_dir':None},f1={'bootstrap_checkpoint':{
        'schema':'f1_rlt_offline_bootstrap_v1','parent_step':1024}}))
    obj.global_step=0
    obj.actor=SimpleNamespace(training_state=lambda:SimpleNamespace(wait=lambda:[state]))
    F1AsyncRunner.init_workers(obj)
    assert obj.global_step==1024
    state['step']=0
    with pytest.raises(ValueError,match='step mismatch'):F1AsyncRunner.init_workers(obj)
    obj.cfg.runner.resume_dir='global_step_1040';obj.global_step=1040
    F1AsyncRunner.init_workers(obj)
    assert obj.global_step==1040
