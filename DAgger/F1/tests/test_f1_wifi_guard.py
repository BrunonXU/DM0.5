import json
from pathlib import Path
import runpy
import time
import pytest
module = runpy.run_path(str(Path(__file__).parents[1]/'scripts/59_f1_wifi_camera_guard.py'))

def test_new_rule_rolls_back_on_verification_failure(monkeypatch):
    fn=module['guarded_install'];g=fn.__globals__;calls=[];checks=iter([False,True,True])
    monkeypatch.setitem(g,'present',lambda:next(checks))
    monkeypatch.setitem(g,'change_rule',lambda op:calls.append(op))
    def fail():raise RuntimeError('observations stale')
    with pytest.raises(RuntimeError,match='stale'):fn(fail)
    assert calls==['-I','-D']

def test_existing_rule_not_removed_or_replaced(monkeypatch):
    fn=module['guarded_install'];g=fn.__globals__;calls=[]
    monkeypatch.setitem(g,'present',lambda:True)
    monkeypatch.setitem(g,'change_rule',lambda op:calls.append(op))
    with pytest.raises(RuntimeError,match='already installed'):fn(lambda:1)
    assert calls==[]

def test_old_camera_or_state_cannot_validate_rule(tmp_path):
    now=time.monotonic_ns();p=tmp_path/'observations.jsonl';check=module['fresh_observation']
    row=dict(capture_started_monotonic_ns=now,available_monotonic_ns=now,observation_id=8,
             state_timestamps_ns=[now]*16,frames=[dict(camera=c,host_received_monotonic_ns=now)
             for c in ('head','left_wrist','right_wrist')])
    p.write_text(json.dumps(row)+'\n');assert check(p,now-1)==8
    row['frames'][1]['host_received_monotonic_ns']=now-2
    p.write_text(json.dumps(row)+'\n');assert check(p,now-1) is None
    row['frames'][1]['host_received_monotonic_ns']=now
    row['state_timestamps_ns'][15]=now-2
    p.write_text(json.dumps(row)+'\n');assert check(p,now-1) is None
