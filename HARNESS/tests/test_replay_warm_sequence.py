"""Unavailable speculation must fall back without changing task configuration."""
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from replay_warm_sequence import ReplayWarmFactory,configuration_identity
from f1_harness.contracts import Task


def inputs():
    return dict(task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal'),
                fixture={'clearance':.01,'frame_chain':{'development_owner_config':'/nonexistent/run/owner.local.json'}},
                spec={'gate':{'runtime':'cache-A','full22':[0]*22},'urdf':'fixed'})


def test_only_cache_destination_is_excluded_from_fixed_configuration():
    values=inputs();expected=configuration_identity(**values)
    same=deepcopy(values);same['spec']['gate']['runtime']='cache-B'
    assert configuration_identity(**same)==expected
    same['spec']['gate']['full22'][0]=1
    assert configuration_identity(**same)!=expected
    same=deepcopy(values);same['fixture']['clearance']=.02
    assert configuration_identity(**same)!=expected
    same=deepcopy(values);same['task']=same['task'].model_copy(update=dict(task_id='new-run',local_corrections=0))
    assert configuration_identity(**same)==expected


@pytest.mark.parametrize('failure',['closed','changed','acquire_error'])
def test_optional_worker_failure_returns_no_prefetch_instead_of_aborting_cycle(failure):
    values=inputs();calls=[]
    def acquire(*_,**__):calls.append(True);raise RuntimeError('lease_unavailable')
    worker=SimpleNamespace(cleanup={'reaped':True} if failure=='closed' else None,acquire=acquire)
    factory=ReplayWarmFactory(worker,configuration_identity(**values))
    if failure=='changed':values['fixture']['clearance']=.02
    lease,receipt=factory(**values,clock_mapping=lambda:{},scope={'session_id':'new'})
    assert lease is None and receipt['reused'] is False
    assert bool(calls)==(failure=='acquire_error')
